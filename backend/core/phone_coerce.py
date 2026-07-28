"""Coerce phone-like values from JSON/API payloads to stripped strings."""
from __future__ import annotations

from typing import Any, Dict, Optional


def coerce_phone_str(value: Any) -> str:
    """
    Normalize a phone field to a stripped string without inventing digits.

    Handles None, str, int, and other numeric scalars. Empty after strip → ``""``.
    Never returns the literal ``"None"`` for missing values.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int):
        return str(value).strip()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)).strip()
        return str(value).strip()
    text = str(value).strip()
    if text.lower() == "none":
        return ""
    return text


def coerce_customer_info_phone(customer_info: Optional[Dict[str, Any]]) -> str:
    """Resolve phone from order ``customer_info`` (``phone`` then ``mobile``)."""
    if not isinstance(customer_info, dict):
        return ""
    phone = coerce_phone_str(customer_info.get("phone"))
    if phone:
        return phone
    return coerce_phone_str(customer_info.get("mobile"))
