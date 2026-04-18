"""
Centralized international phone number normalization.

E.164 standard: +[country_code][number]  — no spaces, no dashes.

Examples
────────
  0570000000      → +966570000000   (Saudi local)
  966570000000    → +966570000000
  +966570000000   → +966570000000
  00966570000000  → +966570000000
  +971501234567   → +971501234567   (UAE)
  +201234567890   → +201234567890   (Egypt)
  +447911123456   → +447911123456   (UK)
  +12125551234    → +12125551234    (USA)

Strategy
────────
1. Saudi heuristics handle unambiguous local numbers (05x, 5x9digits, 966x)
   without needing a region hint.
2. Google libphonenumber (`phonenumbers` package) parses everything else.
3. If the library is not installed (test environments without it), a safe
   sanitized fallback still produces correct E.164 for +prefixed input.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("nahla.phone_utils")

_STRIP_NON_DIGIT_PLUS = re.compile(r"[^\d+]")

try:
    import phonenumbers as _pn
    from phonenumbers import PhoneNumberFormat, NumberParseException
    _LIB_OK = True
except ImportError:
    _LIB_OK = False
    logger.warning(
        "phonenumbers library not installed — international phone validation "
        "is degraded. Run: pip install phonenumbers"
    )


def normalize_to_e164(
    raw: Optional[str],
    default_region: Optional[str] = None,
) -> Optional[str]:
    """
    Normalize a phone number to E.164 format.

    Args:
        raw:            Raw phone string in any format.
        default_region: ISO-3166-1 alpha-2 hint for bare local numbers
                        (e.g. 'SA', 'AE', 'EG', 'GB', 'US').
                        Defaults to 'SA' as the platform's home market —
                        used only when the number has no country prefix.

    Returns:
        E.164 string (e.g. '+966570000000') or None if unparseable.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # ── Step 1: pre-clean ────────────────────────────────────────────────────
    # Keep only digits and a leading + sign
    stripped = _STRIP_NON_DIGIT_PLUS.sub("", text)
    if text.lstrip()[0:2] == "00":
        stripped = "+" + stripped[2:]          # 00xxx → +xxx
    elif text.lstrip().startswith("+"):
        stripped = "+" + stripped              # ensure single leading +

    # ── Step 2: Saudi-specific heuristics (unambiguous local formats) ────────
    # These run before libphonenumber to avoid needing a region hint.
    if not stripped.startswith("+"):
        digits = stripped
        if digits.startswith("966") and len(digits) >= 12:
            # 966XXXXXXXXX → +966XXXXXXXXX
            stripped = "+" + digits
        elif digits.startswith("0") and len(digits) == 10 and digits[1] == "5":
            # 05XXXXXXXX → +96605XXXXXXXX? No — strip leading 0 first
            # 05XXXXXXXX → +9665XXXXXXXX
            stripped = "+966" + digits[1:]
        elif len(digits) == 9 and digits.startswith("5"):
            # 5XXXXXXXX → +9665XXXXXXXX
            stripped = "+966" + digits

    # ── Step 3: libphonenumber parse ─────────────────────────────────────────
    if _LIB_OK:
        # Try direct parse (works for E.164 / international format)
        result = _parse_and_format(stripped, None)
        if result:
            return result

        if not stripped.startswith("+"):
            # Try with explicit region hint
            region = default_region or "SA"
            result = _parse_and_format(stripped, region)
            if result:
                return result

        logger.debug("normalize_to_e164: could not parse '%s'", raw)
        return None

    # ── Fallback: no library — use sanitized stripped string ─────────────────
    # Correct for numbers that already arrived in E.164 or close to it.
    if stripped.startswith("+") and len(stripped) >= 9:
        return stripped
    return None


def _parse_and_format(number: str, region: Optional[str]) -> Optional[str]:
    """Attempt phonenumbers.parse(); return E.164 on success, None on failure."""
    try:
        parsed = _pn.parse(number, region)
        if _pn.is_valid_number(parsed):
            return _pn.format_number(parsed, PhoneNumberFormat.E164)
        return None
    except Exception:
        return None


def is_valid_e164(phone: Optional[str]) -> bool:
    """Return True iff phone is a syntactically valid E.164 number."""
    if not phone or not phone.startswith("+"):
        return False
    digits = phone[1:]
    if not digits.isdigit() or len(digits) < 7 or len(digits) > 15:
        return False
    if _LIB_OK:
        return _parse_and_format(phone, None) is not None
    return True


def normalize_phone_compat(raw: object) -> str:
    """
    Backward-compatible wrapper used by legacy callers.

    Returns E.164 string on success, '' (empty string, falsy) on failure.
    This preserves the original normalize_phone(raw) -> str contract.
    """
    result = normalize_to_e164(str(raw or "").strip())
    return result or ""
