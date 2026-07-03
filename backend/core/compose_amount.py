"""Safe parsing/formatting for order totals in compose templates."""
from __future__ import annotations

from typing import Any, Optional

_CURRENCY_MARKERS = ("ر.س", "SAR", "ريال", "sr")


def parse_compose_amount(value: Any) -> Optional[float]:
    """Parse a numeric order total from int/float or localized strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amt = float(value)
        return amt if amt > 0 else None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text
    for marker in _CURRENCY_MARKERS:
        cleaned = cleaned.replace(marker, "")
    cleaned = cleaned.replace(",", "").strip()
    if not cleaned:
        return None
    token = cleaned.split()[0]
    try:
        amt = float(token)
    except (TypeError, ValueError):
        return None
    return amt if amt > 0 else None


def format_order_total_display(total: Any, currency: str = "SAR") -> Optional[str]:
    """Return a safe total fragment for customer-facing compose, or None to omit."""
    parsed = parse_compose_amount(total)
    if parsed is not None:
        return f"{parsed:.2f} {currency}"
    raw = str(total or "").strip()
    if not raw:
        return None
    if any(marker in raw for marker in _CURRENCY_MARKERS):
        return raw
    return None
