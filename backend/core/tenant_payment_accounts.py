"""
core/tenant_payment_accounts.py
───────────────────────────────
Single source of truth for "what are this merchant's official
payment accounts?".

Why this module exists
──────────────────────
Tenant 33 #48 (May 2026) — production complaint:

    A customer mentioned a transfer / amount in plain text without
    sending any payment receipt to the store. The bot assumed the
    payment was confirmed, the receipt arrived, and the order is
    ready for shipping.

The merchant's instruction was unambiguous:

    "ذكر مبلغ أو كلمة 'حولت' لا يعني أن الدفع تأكد. ولا يجوز اعتبار
     الطلب مدفوعًا إلا عند وجود payment evidence حقيقي ومطابق
     لحساب التاجر الرسمي."

So the AI must be able to answer two distinct questions, not one:

    Q1. Did the customer SEND payment evidence? (image / PDF receipt)
    Q2. Does that evidence MATCH this merchant's official accounts?

Q1 is already answered by ``core.payment_evidence.classify_payment_evidence``.
This module covers Q2 — given a tenant_id, what IBANs and beneficiary
names are registered as theirs, so a downstream classifier can compare
the receipt's OCR contents against them?

We deliberately keep this read-only and tenant-scoped:

  * Reads ``MerchantKnowledgeSection`` rows where ``kind='bank_transfer'``
    or ``kind='payment_method'`` and ``is_active=True``.
  * Extracts Saudi IBANs (SA + 22 digits, with optional whitespace
    or dashes) from ``body`` + ``title``.
  * Extracts beneficiary tokens from ``metadata_json.beneficiary_name``
    and from the body when prefixed with "اسم المستفيد".
  * Returns a typed snapshot the caller compares against; the caller
    decides whether a "no accounts configured" tenant should still
    auto-confirm receipts (back-compat) or require a match.

The module is pure-Python beyond the SQLAlchemy query, never raises
into the caller, and degrades to "no accounts available" on any
unexpected DB / import failure. Safe to call from any path.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger("nahla.tenant_payment_accounts")


# ── Saudi IBAN regex ────────────────────────────────────────────────
# SA + 22 digits. Banks print them with various spacing patterns:
#   * SA0380000000608010167519
#   * SA03 8000 0000 6080 1016 7519
#   * SA03-8000-0000-6080-1016-7519
# We allow whitespace + hyphens between digit groups, then strip them
# in the extracted form so equality comparison is canonical.
_IBAN_RE = re.compile(
    r"\bsa\s*\d{2}(?:[\s\-]*\d){20}\b",
    re.IGNORECASE,
)


# ── Beneficiary name capture ────────────────────────────────────────
# When merchants type "اسم المستفيد: عبدالله محمد" in the
# bank_transfer KB body we want the value half. Same for the English
# "Beneficiary: ...". Capture is bounded — beneficiaries are short.
_BENEFICIARY_LABEL_RE = re.compile(
    r"(?:اسم\s*المستفيد|المستفيد|اسم\s*صاحب\s*الحساب|صاحب\s*الحساب|"
    r"beneficiary(?:\s*name)?|account\s*holder)"
    r"\s*[:\-–—]\s*(?P<name>[^\n\r]+?)(?=\s*(?:[\n\r]|$|"
    r"رقم|اسم\s*البنك|البنك\s*:|iban\s*[:#]|sa\s*\d|$))",
    re.IGNORECASE,
)


# ── Light Arabic normaliser (mirrors core.payment_intent + core.payment_evidence) ─
_AR_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")


def _normalise(text: Optional[str]) -> str:
    if not text:
        return ""
    try:
        t = _AR_DIACRITICS.sub("", str(text))
        t = t.replace("ـ", "")
        t = (
            t.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ى", "ي")
             .replace("ة", "ه")
        )
        return t.lower().strip()
    except Exception:
        return ""


def canonical_iban(value: str) -> str:
    """Return the canonical (no whitespace, uppercase) form of a
    Saudi IBAN string. Returns empty string when the input doesn't
    look like a Saudi IBAN. Pure function, never raises."""
    if not value:
        return ""
    try:
        compact = re.sub(r"[\s\-]+", "", str(value)).upper()
    except Exception:
        return ""
    if not compact.startswith("SA"):
        return ""
    digits_part = compact[2:]
    if len(digits_part) != 22 or not digits_part.isdigit():
        return ""
    return compact


def extract_ibans(text: Optional[str]) -> List[str]:
    """Pull all Saudi IBANs from a free-form text blob, returned in
    canonical form (no whitespace / hyphens, uppercase). Order
    preserved, duplicates removed."""
    if not text:
        return []
    out: List[str] = []
    seen: set = set()
    for m in _IBAN_RE.finditer(str(text)):
        canon = canonical_iban(m.group(0))
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def extract_beneficiaries(text: Optional[str]) -> List[str]:
    """Pull beneficiary-name candidates from labelled body text.
    Returns the *normalised* form so equality / substring checks
    don't trip on alef variants. Bounded length; merchants who paste
    paragraphs into the field still produce a reasonable snippet."""
    if not text:
        return []
    out: List[str] = []
    seen: set = set()
    for m in _BENEFICIARY_LABEL_RE.finditer(str(text)):
        raw = (m.group("name") or "").strip()
        if not raw or len(raw) > 80:
            continue
        norm = _normalise(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


# ── Public types ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantPaymentAccounts:
    """Snapshot of a tenant's official payment accounts.

    All fields default to empty so a tenant without configured
    accounts produces a falsy ``has_accounts`` — callers can use
    that to keep back-compatible behaviour (e.g. don't enforce
    matching when nothing is on file).
    """

    ibans: Tuple[str, ...] = field(default_factory=tuple)
    beneficiaries: Tuple[str, ...] = field(default_factory=tuple)
    bank_brands: Tuple[str, ...] = field(default_factory=tuple)
    section_ids: Tuple[int, ...] = field(default_factory=tuple)

    @property
    def has_accounts(self) -> bool:
        return bool(self.ibans or self.beneficiaries)

    def to_log_dict(self) -> dict:
        return {
            "iban_count":           len(self.ibans),
            "beneficiary_count":    len(self.beneficiaries),
            "bank_brand_count":     len(self.bank_brands),
            "has_accounts":         self.has_accounts,
        }


# ── Bank-brand vocabulary (subset of core.payment_evidence's list) ──
# Used purely as an additional context signal — NEVER as the sole
# match criterion. A receipt that mentions "الراجحي" but no IBAN/
# beneficiary stays unverified.
_KNOWN_BANK_BRANDS: Tuple[str, ...] = tuple(_normalise(s) for s in (
    "الراجحي", "rajhi", "alrajhi",
    "الاهلي", "alahli", "ncb", "snb",
    "الانماء", "alinma",
    "البلاد", "albilad",
    "ساب", "sab", "sabb",
    "العربي", "anb",
    "الفرنسي",
    "الرياض",
    "stcpay", "stc pay",
))


def _scan_bank_brands(text: str) -> List[str]:
    if not text:
        return []
    norm = _normalise(text)
    out: List[str] = []
    seen: set = set()
    for brand in _KNOWN_BANK_BRANDS:
        if brand and brand in norm and brand not in seen:
            seen.add(brand)
            out.append(brand)
    return out


# ── Loader ──────────────────────────────────────────────────────────


def load_tenant_payment_accounts(
    db: Any,
    *,
    tenant_id: int,
) -> TenantPaymentAccounts:
    """Load the tenant's official payment accounts from KB sections.

    Sources:
      * ``MerchantKnowledgeSection`` rows where
        ``kind in ('bank_transfer', 'payment_method')`` and
        ``is_active = True``.
      * IBANs are extracted from ``body`` + ``title``.
      * Beneficiary names are extracted from labelled body text and
        from ``metadata_json.beneficiary_name`` when present.

    Never raises. Returns an empty ``TenantPaymentAccounts`` on any
    DB / import failure or when the tenant has no configured
    accounts.
    """
    if db is None or not tenant_id:
        return TenantPaymentAccounts()
    try:
        from database.models import MerchantKnowledgeSection  # noqa: PLC0415
    except Exception:
        try:
            from models import MerchantKnowledgeSection  # type: ignore  # noqa: PLC0415
        except Exception as exc:
            logger.debug(
                "[TENANT_PAY_ACCOUNTS] model import failed tenant=%s err=%s",
                tenant_id, exc,
            )
            return TenantPaymentAccounts()

    try:
        rows = (
            db.query(MerchantKnowledgeSection)
              .filter(MerchantKnowledgeSection.tenant_id == int(tenant_id))
              .filter(MerchantKnowledgeSection.kind.in_(
                  ("bank_transfer", "payment_method"),
              ))
              .filter(MerchantKnowledgeSection.is_active.is_(True))
              .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[TENANT_PAY_ACCOUNTS] query failed tenant=%s err=%s",
            tenant_id, exc,
        )
        return TenantPaymentAccounts()

    iban_set: List[str] = []
    seen_iban: set = set()
    benef_set: List[str] = []
    seen_benef: set = set()
    bank_brand_set: List[str] = []
    seen_brand: set = set()
    section_ids: List[int] = []

    for row in rows or []:
        try:
            sid = int(getattr(row, "id", 0) or 0)
            title = str(getattr(row, "title", "") or "")
            body = str(getattr(row, "body", "") or "")
            md = getattr(row, "metadata_json", None) or {}
        except Exception:
            continue
        if sid:
            section_ids.append(sid)

        # Title and body are both fair game — merchants sometimes
        # paste IBANs into the title (e.g. "حساب الراجحي SA03...").
        for blob in (title, body):
            for iban in extract_ibans(blob):
                if iban not in seen_iban:
                    seen_iban.add(iban)
                    iban_set.append(iban)
            for benef in extract_beneficiaries(blob):
                if benef not in seen_benef:
                    seen_benef.add(benef)
                    benef_set.append(benef)
            for brand in _scan_bank_brands(blob):
                if brand not in seen_brand:
                    seen_brand.add(brand)
                    bank_brand_set.append(brand)

        # Beneficiary name from structured metadata wins over body
        # parsing when present — the merchant explicitly typed it
        # into a dedicated field.
        if isinstance(md, dict):
            md_benef = (md.get("beneficiary_name") or "").strip()
            if md_benef:
                norm = _normalise(md_benef)
                if norm and norm not in seen_benef:
                    seen_benef.add(norm)
                    benef_set.append(norm)
            md_iban = (md.get("iban") or "").strip()
            if md_iban:
                canon = canonical_iban(md_iban)
                if canon and canon not in seen_iban:
                    seen_iban.add(canon)
                    iban_set.append(canon)

    return TenantPaymentAccounts(
        ibans=tuple(iban_set),
        beneficiaries=tuple(benef_set),
        bank_brands=tuple(bank_brand_set),
        section_ids=tuple(section_ids),
    )


# ── Match check ─────────────────────────────────────────────────────


def receipt_matches_tenant_accounts(
    *,
    accounts: TenantPaymentAccounts,
    receipt_text: Optional[str],
) -> dict:
    """Compare an OCR / PDF / caption blob against a tenant's
    registered accounts. Returns a structured verdict the caller
    consumes when deciding whether to flip ``payment_receipt_received``
    to True.

    Verdict shape::

        {
          "status":       "match" | "mismatch" | "no_tenant_accounts"
                          | "no_signal_in_receipt",
          "matched_iban": <iban or "">,
          "matched_beneficiary": <name or "">,
          "receipt_ibans": [...],
          "receipt_beneficiaries": [...],
        }

    Statuses:

      * ``match``               — at least one IBAN OR beneficiary
                                  present in the receipt is also
                                  registered as the tenant's account.
                                  Caller may proceed to flip
                                  ``payment_receipt_received=True``.

      * ``mismatch``            — receipt has clear IBAN/beneficiary
                                  signals BUT none of them match
                                  any registered tenant account.
                                  Caller MUST NOT flip
                                  ``payment_receipt_received=True``.

      * ``no_tenant_accounts``  — the tenant has no registered
                                  accounts on file. Caller falls
                                  back to legacy behaviour (don't
                                  break tenants without KB
                                  configuration).

      * ``no_signal_in_receipt`` — receipt blob has neither IBANs
                                   nor beneficiary tokens. The
                                   caller can keep the existing
                                   text-evidence verdict but should
                                   NOT claim the merchant's account
                                   was matched.

    Pure function, never raises.
    """
    receipt_ibans = extract_ibans(receipt_text)
    receipt_benefs = extract_beneficiaries(receipt_text)

    if not accounts.has_accounts:
        return {
            "status":              "no_tenant_accounts",
            "matched_iban":        "",
            "matched_beneficiary": "",
            "receipt_ibans":       receipt_ibans,
            "receipt_beneficiaries": receipt_benefs,
        }

    if not receipt_ibans and not receipt_benefs:
        return {
            "status":              "no_signal_in_receipt",
            "matched_iban":        "",
            "matched_beneficiary": "",
            "receipt_ibans":       receipt_ibans,
            "receipt_beneficiaries": receipt_benefs,
        }

    # IBAN match wins — exact canonical equality. We ALSO check the
    # raw receipt text against every registered IBAN so a slightly
    # different formatting (extra space, typo in spacing) still
    # matches the canonical one we extracted on the tenant side.
    receipt_blob_norm = _normalise(receipt_text or "")
    for iban in accounts.ibans:
        if iban in receipt_ibans:
            return {
                "status":              "match",
                "matched_iban":        iban,
                "matched_beneficiary": "",
                "receipt_ibans":       receipt_ibans,
                "receipt_beneficiaries": receipt_benefs,
            }
        # Fallback: substring match on the raw blob in case the
        # extractor regex missed it (e.g. weird whitespace).
        if iban.lower() in (receipt_text or "").lower().replace(" ", ""):
            return {
                "status":              "match",
                "matched_iban":        iban,
                "matched_beneficiary": "",
                "receipt_ibans":       receipt_ibans + [iban],
                "receipt_beneficiaries": receipt_benefs,
            }

    # Beneficiary substring match against the normalised blob.
    # We require ALL tokens of the registered beneficiary to appear
    # in the blob — otherwise "محمد" alone would match any
    # transfer to anyone named محمد.
    for benef in accounts.beneficiaries:
        tokens = [t for t in benef.split() if len(t) >= 2]
        if not tokens:
            continue
        if all(t in receipt_blob_norm for t in tokens):
            return {
                "status":              "match",
                "matched_iban":        "",
                "matched_beneficiary": benef,
                "receipt_ibans":       receipt_ibans,
                "receipt_beneficiaries": receipt_benefs,
            }

    return {
        "status":              "mismatch",
        "matched_iban":        "",
        "matched_beneficiary": "",
        "receipt_ibans":       receipt_ibans,
        "receipt_beneficiaries": receipt_benefs,
    }


__all__ = [
    "TenantPaymentAccounts",
    "canonical_iban",
    "extract_ibans",
    "extract_beneficiaries",
    "load_tenant_payment_accounts",
    "receipt_matches_tenant_accounts",
]
