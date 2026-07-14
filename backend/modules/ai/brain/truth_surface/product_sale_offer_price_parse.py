"""
product_sale_offer_price_parse.py
─────────────────────────────────
Canonical strict catalog sale price parsing for product_sale_offer.

Loader, repository post-processing, and parity tests MUST use this module —
not core.catalog.normalize_catalog_price_amount directly.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

_NUMERIC_PRICE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def extract_price_raw_from_json_value(value: Any) -> Optional[str]:
    """Mirror PostgreSQL jsonb_typeof extraction for a stored metadata value."""
    if value is None:
        return None
    if isinstance(value, dict):
        amount = value.get("amount")
        if amount is None or isinstance(amount, (dict, list)):
            return None
        return extract_price_raw_from_json_value(amount)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if float(value).is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def normalize_extracted_price_raw(raw: Optional[str]) -> Optional[str]:
    """Canonical numeric string: trim → remove commas → validate → format."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace(",", "")
    if not _NUMERIC_PRICE_RE.match(normalized):
        return None
    try:
        num = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    if num <= 0:
        return None
    if num == num.to_integral_value():
        return str(int(num))
    text_out = format(num, "f").rstrip("0").rstrip(".")
    return text_out or str(num)


def canonical_price_string(value: Any) -> Optional[str]:
    """Single canonical entry for metadata values and SQL-extracted raw strings."""
    return normalize_extracted_price_raw(extract_price_raw_from_json_value(value))


def is_strict_sale_normalized_pair(
    sale_normalized: Optional[str],
    regular_normalized: Optional[str],
) -> bool:
    if not sale_normalized or not regular_normalized:
        return False
    try:
        sale_d = Decimal(str(sale_normalized))
        regular_d = Decimal(str(regular_normalized))
    except (InvalidOperation, ValueError):
        return False
    return sale_d > 0 and regular_d > 0 and sale_d < regular_d


def strict_sale_from_metadata(meta: dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool]:
    """Return canonical (sale, regular, is_strict_sale) from extra_metadata."""
    sale = canonical_price_string(meta.get("sale_price"))
    regular = canonical_price_string(meta.get("regular_price"))
    on_sale = is_strict_sale_normalized_pair(sale, regular)
    return sale, regular, on_sale


# Back-compat alias — all call sites should prefer canonical_price_string.
parse_metadata_price_value = canonical_price_string


__all__ = [
    "canonical_price_string",
    "extract_price_raw_from_json_value",
    "is_strict_sale_normalized_pair",
    "normalize_extracted_price_raw",
    "parse_metadata_price_value",
    "strict_sale_from_metadata",
]
