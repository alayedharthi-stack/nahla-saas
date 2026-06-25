"""
core/order_amount_display.py
────────────────────────────
Resolve dashboard order amounts from persisted totals vs line_items.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.wa_cart_line_items import cart_total_amount


def parse_amount_sar(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return parse_amount_sar(value.get("amount") or value.get("value") or 0)
    text = str(value).replace("ر.س", "").replace(",", "").replace("SAR", "").strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def resolve_display_amount_sar(
    *,
    source: str,
    line_items: Optional[List[Dict[str, Any]]],
    persisted_total: Any,
) -> Tuple[float, float, bool]:
    """
    Return ``(display_amount, persisted_amount, persisted_stale)``.

    WhatsApp orders prefer the sum of priced line_items when available.
    Other sources keep the persisted ``Order.total`` value.
    """
    persisted = round(parse_amount_sar(persisted_total), 2)
    if source != "whatsapp":
        return persisted, persisted, False

    items = list(line_items or [])
    computed = cart_total_amount(items)
    if computed is None or computed <= 0:
        return persisted, persisted, False

    display = round(computed, 2)
    stale = persisted > 0 and abs(display - persisted) > 0.009
    return display, persisted, stale


__all__ = [
    "parse_amount_sar",
    "resolve_display_amount_sar",
]
