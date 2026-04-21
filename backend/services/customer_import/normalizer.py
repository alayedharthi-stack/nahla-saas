"""
services/customer_import/normalizer.py
──────────────────────────────────────
Per-row normalization for the import wizard.

Takes one parsed-row dict + the user's column mapping and produces a
`NormalizedRow` ready for dedupe classification:

    - phone       → E.164 (utils.phone_utils.normalize_to_e164)
    - email       → trimmed + lowercased
    - name        → trimmed, collapsed whitespace
    - city/notes  → trimmed
    - source      → trimmed (defaults to "manual_import" if missing)

Also exposes `suggest_column_mapping(headers)` which heuristically
matches Arabic / English header names to canonical fields so the
wizard can pre-fill step 2 for the merchant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils.phone_utils import normalize_to_e164


# Canonical fields supported by the import. Phone is required for any
# row to even be attempted; everything else is optional.
REQUIRED_FIELDS: tuple = ("phone",)
OPTIONAL_FIELDS: tuple = ("name", "email", "city", "notes", "source")
SUPPORTED_FIELDS: tuple = REQUIRED_FIELDS + OPTIONAL_FIELDS


# Header → canonical field heuristics. Lower-cased and stripped of
# punctuation before matching. Arabic synonyms cover the most common
# Excel column names merchants use.
_HEADER_HINTS: Dict[str, tuple] = {
    "name": (
        "name", "full name", "fullname", "customer", "customer name",
        "client", "client name",
        "اسم", "الاسم", "اسم العميل", "اسم الزبون", "اسم الكامل",
    ),
    "phone": (
        "phone", "phone number", "mobile", "mobile number", "cell",
        "whatsapp", "whatsapp number", "contact", "tel", "telephone",
        "msisdn", "wa", "wa number",
        "هاتف", "الهاتف", "جوال", "الجوال", "رقم الجوال", "رقم الهاتف",
        "موبايل", "الموبايل", "رقم", "واتساب", "رقم واتساب",
    ),
    "email": (
        "email", "e-mail", "mail", "email address",
        "بريد", "البريد", "ايميل", "الإيميل", "البريد الإلكتروني",
        "البريد الالكتروني",
    ),
    "city": (
        "city", "town", "region",
        "مدينة", "المدينة", "بلد", "البلد", "المنطقة",
    ),
    "notes": (
        "notes", "note", "remarks", "comment", "comments", "description",
        "ملاحظة", "ملاحظات", "تعليق", "وصف",
    ),
    "source": (
        "source", "channel", "origin",
        "مصدر", "المصدر", "قناة", "القناة",
    ),
}

_PUNCT_RE = re.compile(r"[\s_\-\.\:،\(\)\[\]\{\}\\/]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


@dataclass
class NormalizedRow:
    row_index: int
    raw: Dict[str, str] = field(default_factory=dict)

    name: str = ""
    phone_raw: str = ""
    normalized_phone: str = ""
    email: str = ""
    city: str = ""
    notes: str = ""
    source: str = "manual_import"

    invalid_reasons: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.invalid_reasons

    def to_dict(self) -> Dict:
        return {
            "row_index": self.row_index,
            "raw": self.raw,
            "name": self.name,
            "phone_raw": self.phone_raw,
            "normalized_phone": self.normalized_phone,
            "email": self.email,
            "city": self.city,
            "notes": self.notes,
            "source": self.source,
            "invalid_reasons": list(self.invalid_reasons),
        }


# ── Public API ───────────────────────────────────────────────────────────────

def normalize_row(
    *,
    row_index: int,
    raw: Dict[str, str],
    mapping: Dict[str, str],
    default_region: Optional[str] = None,
) -> NormalizedRow:
    """Apply the user's column mapping + per-field cleaners. Always
    returns a `NormalizedRow` — invalid input populates `invalid_reasons`
    instead of raising, so the wizard can show every problem row at
    once."""
    out = NormalizedRow(row_index=row_index, raw=dict(raw))

    out.name      = _clean_name(_lookup(raw, mapping.get("name")))
    out.email     = _clean_email(_lookup(raw, mapping.get("email")))
    out.city      = _clean_text(_lookup(raw, mapping.get("city")))
    out.notes     = _clean_text(_lookup(raw, mapping.get("notes")))

    raw_source    = _clean_text(_lookup(raw, mapping.get("source")))
    out.source    = raw_source or "manual_import"

    out.phone_raw = _clean_text(_lookup(raw, mapping.get("phone")))
    if not out.phone_raw:
        out.invalid_reasons.append("missing_phone")
    else:
        normalized = normalize_to_e164(
            out.phone_raw, default_region=default_region or "SA",
        )
        if not normalized:
            out.invalid_reasons.append("invalid_phone_format")
        else:
            out.normalized_phone = normalized

    if out.email and not _is_plausible_email(out.email):
        out.invalid_reasons.append("invalid_email_format")
        # Drop the bad email so it is never persisted.
        out.email = ""

    return out


def suggest_column_mapping(headers: List[str]) -> Dict[str, str]:
    """Heuristic best-guess mapping the wizard pre-fills on step 2.
    Merchants can change any field; this only saves them clicks."""
    mapping: Dict[str, str] = {}
    used: set = set()
    normalized = {h: _norm_for_match(h) for h in headers}
    for canonical, hints in _HEADER_HINTS.items():
        for h, ncomp in normalized.items():
            if h in used:
                continue
            if any(_norm_for_match(hint) == ncomp for hint in hints):
                mapping[canonical] = h
                used.add(h)
                break
    return mapping


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lookup(raw: Dict[str, str], header: Optional[str]) -> str:
    if not header:
        return ""
    val = raw.get(header)
    if val is None:
        return ""
    return str(val)


def _clean_text(value: str) -> str:
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


def _clean_name(value: str) -> str:
    cleaned = _clean_text(value)
    # Avoid storing literal "None" / "N/A" placeholders some merchants
    # leave in their Excel exports.
    if cleaned.lower() in {"none", "n/a", "na", "null", "-"}:
        return ""
    return cleaned


def _clean_email(value: str) -> str:
    cleaned = _clean_text(value).lower()
    if cleaned.lower() in {"none", "n/a", "na", "null", "-"}:
        return ""
    return cleaned


def _is_plausible_email(value: str) -> bool:
    # Intentionally simple — real validation happens at delivery time.
    if "@" not in value:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return True


def _norm_for_match(value: str) -> str:
    """Lowercase + strip punctuation/whitespace for header matching.
    Treats Arabic alef variants as equivalent."""
    if not value:
        return ""
    s = str(value).strip().lower()
    s = _PUNCT_RE.sub("", s)
    # Normalize alef hamza variants commonly seen in Arabic headers.
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي")
    s = s.replace("ة", "ه")
    return s
