"""Shared Layer 3 evidence helpers (no harness/scoring imports)."""
from __future__ import annotations

from typing import Any


def resolve_focus_product_id(focus: Any) -> str:
    """Match ``product_focus_identity`` priority: external_id → id → product_id → sku."""
    if not isinstance(focus, dict):
        return str(focus or "").strip()
    for key in ("external_id", "id", "product_id", "sku"):
        val = str(focus.get(key) or "").strip()
        if val:
            return val
    title = str(focus.get("title") or focus.get("display_label") or "").strip().lower()
    return title


__all__ = ["resolve_focus_product_id"]
