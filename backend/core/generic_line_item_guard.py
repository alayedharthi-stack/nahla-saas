"""Block order create/sync when line items are generic or ungrounded placeholders."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

GENERIC_PLACEHOLDER_PRODUCT_NAMES = frozenset(
    {
        "منتج",
        "product",
        "item",
        "شيء",
        "شي",
        "غير محدد",
        "المطلوب",
        "صنف",
        "سلعة",
    }
)

GENERIC_LINE_ITEM_CLARIFICATION_AR = (
    "أي منتج تقصد بالضبط؟ تقدر تختار من الكتالوج أو تكتب اسم المنتج."
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")


def _norm_name(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).strip().lower())
    t = _NORM_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def is_generic_placeholder_product_name(name: str) -> bool:
    """True when line-item name is an ungrounded placeholder per policy §6."""
    return _norm_name(name) in {_norm_name(x) for x in GENERIC_PLACEHOLDER_PRODUCT_NAMES}


def _item_display_name(item: Dict[str, Any]) -> str:
    return str(
        item.get("product_name")
        or item.get("title")
        or item.get("name")
        or ""
    ).strip()


def _item_product_ref(item: Dict[str, Any]) -> str:
    return str(
        item.get("product_id")
        or item.get("sku")
        or item.get("external_id")
        or item.get("product_retailer_id")
        or ""
    ).strip()


def is_grounded_line_item(item: Dict[str, Any]) -> bool:
    """True when the line item has non-placeholder product identity."""
    if not isinstance(item, dict):
        return False
    name = _item_display_name(item)
    if name and not is_generic_placeholder_product_name(name):
        return True
    product_ref = _item_product_ref(item)
    if product_ref and not is_generic_placeholder_product_name(product_ref):
        return True
    return False


def line_items_contain_only_generic_placeholders(
    line_items: Sequence[dict],
) -> bool:
    if not line_items:
        return False
    for item in line_items:
        if not isinstance(item, dict):
            return False
        if is_grounded_line_item(item):
            return False
    return True


@dataclass(frozen=True)
class LineItemGroundingDecision:
    allowed: bool
    reason: str = ""
    grounded_count: int = 0
    generic_count: int = 0


def evaluate_line_item_grounding(
    line_items: Optional[Sequence[dict]],
) -> LineItemGroundingDecision:
    """Final-boundary guard: block generic-only or mixed generic carts."""
    items = [i for i in (line_items or []) if isinstance(i, dict)]
    if not items:
        return LineItemGroundingDecision(allowed=True, reason="empty")

    grounded = sum(1 for item in items if is_grounded_line_item(item))
    generic = len(items) - grounded

    if grounded == 0:
        return LineItemGroundingDecision(
            allowed=False,
            reason="generic_ungrounded_line_items",
            grounded_count=0,
            generic_count=generic,
        )
    if generic > 0:
        return LineItemGroundingDecision(
            allowed=False,
            reason="mixed_generic_line_items",
            grounded_count=grounded,
            generic_count=generic,
        )
    return LineItemGroundingDecision(
        allowed=True,
        reason="grounded",
        grounded_count=grounded,
        generic_count=0,
    )


def collect_line_item_candidates(
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Line items from prep/brain without catalog resolution (pre-sync probe)."""
    out: List[Dict[str, Any]] = []
    for container in (order_prep, brain_state or {}):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        out.append(dict(item))
    return out


def evaluate_order_prep_line_item_grounding(
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
) -> LineItemGroundingDecision:
    return evaluate_line_item_grounding(
        collect_line_item_candidates(order_prep, brain_state)
    )


__all__ = [
    "GENERIC_LINE_ITEM_CLARIFICATION_AR",
    "GENERIC_PLACEHOLDER_PRODUCT_NAMES",
    "LineItemGroundingDecision",
    "collect_line_item_candidates",
    "evaluate_line_item_grounding",
    "evaluate_order_prep_line_item_grounding",
    "is_generic_placeholder_product_name",
    "is_grounded_line_item",
    "line_items_contain_only_generic_placeholders",
]
