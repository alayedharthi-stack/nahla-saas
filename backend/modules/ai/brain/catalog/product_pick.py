"""
catalog/product_pick.py
───────────────────────
Deterministic product pick from CatalogNavigator group-product pages.

Runs before generic selection_context / list_pick so numeric picks map to
last_presented_group_products, not last_search_candidates.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ..decision.actions import ACTION_PROPOSE_DRAFT_ORDER
from ..types import BrainContext, Decision
from .navigation_signals import is_navigation_more_request

logger = logging.getLogger("nahla.brain.catalog.product_pick")

CANDIDATE_SOURCE = "catalog_navigation_group_products"
DECISION_SOURCE = "catalog_navigation_product_pick"

_DIA = r"[\u064B-\u065F\u0640]"

_ORDINAL_INDEX: Dict[str, int] = {
    "الاول": 1, "الأول": 1, "اول": 1, "١": 1, "1": 1,
    "الثاني": 2, "الثانية": 2, "ثاني": 2, "٢": 2, "2": 2,
    "الثالث": 3, "الثالثة": 3, "ثالث": 3, "٣": 3, "3": 3,
    "4": 4, "٤": 4, "5": 5, "٥": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
}

_NUMERIC_PRODUCT_PICK_RE = re.compile(
    r"^(?:اب[ىي]|أب[ىي]|اختر|اختار|اريد|أريد|ودي|this|that)?\s*"
    r"(?:ال)?(\d+|الاول|الأول|اول|الثاني|الثانية|ثاني|الثالث|الثالثة|ثالث|"
    r"١|٢|٣|1|2|3|4|5|6|7|8|9|10)\s*[?؟.]?\s*$",
    re.UNICODE | re.IGNORECASE,
)


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(_DIA, "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def has_active_group_products_context(state: Any) -> bool:
    group = getattr(state, "current_catalog_group", None)
    if not isinstance(group, dict) or not group:
        return False
    presented = list(getattr(state, "last_presented_group_products", None) or [])
    return bool(presented)


def get_presented_group_products(state: Any) -> List[Dict[str, Any]]:
    return list(getattr(state, "last_presented_group_products", None) or [])


def is_group_product_pick_message(message: str) -> bool:
    norm = _normalize_ar(message)
    if not norm:
        return False
    if is_navigation_more_request(message):
        return False
    return bool(_NUMERIC_PRODUCT_PICK_RE.match(norm))


def extract_group_product_pick_index(message: str) -> Optional[int]:
    norm = _normalize_ar(message)
    if not norm:
        return None
    m = _NUMERIC_PRODUCT_PICK_RE.match(norm)
    if not m:
        return None
    token = (m.group(1) or "").strip().lower()
    if token.isdigit():
        return int(token)
    return _ORDINAL_INDEX.get(token)


def build_rich_forced_product(
    product: Dict[str, Any],
    *,
    selected_index: int,
    source_group: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = dict(product or {})
    group = dict(source_group or {})
    title = str(row.get("display_label") or row.get("title") or "").strip()
    variant_id = str(
        row.get("variant_id")
        or row.get("default_variant_id")
        or row.get("selected_variant_id")
        or ""
    ).strip()
    option_prefill: Dict[str, Any] = {}
    for key in ("size", "weight", "unit", "variant_name", "option_label"):
        val = str(row.get(key) or "").strip()
        if val:
            option_prefill[key] = val
    return {
        "product_id": str(row.get("id") or row.get("product_id") or "").strip(),
        "id": str(row.get("id") or row.get("product_id") or "").strip(),
        "external_id": str(row.get("external_id") or row.get("id") or "").strip(),
        "variant_id": variant_id,
        "default_variant_id": str(row.get("default_variant_id") or variant_id or "").strip(),
        "title": title or str(row.get("title") or "").strip(),
        "display_label": title or str(row.get("title") or "").strip(),
        "size": row.get("size"),
        "weight": row.get("weight"),
        "unit": row.get("unit"),
        "variant_name": row.get("variant_name"),
        "option_label": row.get("option_label"),
        "price": row.get("price"),
        "sale_price": row.get("sale_price"),
        "can_checkout": row.get("can_checkout", row.get("orderable", True)),
        "orderable": row.get("orderable", row.get("can_checkout", True)),
        "source_group": {
            "group_id": group.get("group_id") or group.get("id"),
            "group_slug": group.get("group_slug") or group.get("slug"),
            "group_name": group.get("group_name") or group.get("name") or group.get("label"),
        },
        "selected_index": selected_index,
        "candidate_source": CANDIDATE_SOURCE,
        "option_prefill": option_prefill,
    }


def try_catalog_navigation_product_pick_decision(ctx: BrainContext) -> Optional[Decision]:
    """Resolve numeric picks against the current Navigator group-product page."""
    state = ctx.state
    from .navigator_exit import navigator_should_yield_to_order_flow  # noqa: PLC0415

    if navigator_should_yield_to_order_flow(state):
        return None
    if not has_active_group_products_context(state):
        return None
    msg = ctx.message or ""
    if not is_group_product_pick_message(msg):
        return None

    index = extract_group_product_pick_index(msg)
    if index is None or index < 1:
        return None

    presented = get_presented_group_products(state)
    if index > len(presented):
        logger.info(
            "[CATALOG_NAVIGATOR] product_pick_out_of_page tenant=%s index=%d page_size=%d",
            getattr(ctx, "tenant_id", None),
            index,
            len(presented),
        )
        return None

    product = presented[index - 1]
    if not isinstance(product, dict) or not product:
        return None

    source_group = getattr(state, "current_catalog_group", None)
    forced = build_rich_forced_product(
        product,
        selected_index=index,
        source_group=source_group if isinstance(source_group, dict) else None,
    )
    if not forced.get("external_id") and not forced.get("id"):
        return None

    logger.info(
        "[CATALOG_NAVIGATOR] product_pick tenant=%s index=%d title=%r variant=%s source=%s",
        getattr(ctx, "tenant_id", None),
        index,
        forced.get("title"),
        forced.get("variant_id") or "-",
        CANDIDATE_SOURCE,
    )
    try:
        from .numeric_ownership import (  # noqa: PLC0415
            NUMERIC_OWNER_GROUP_PRODUCTS_PAGE,
            get_button_provenance,
            log_numeric_ownership,
        )

        log_numeric_ownership(
            ctx,
            numeric_owner=NUMERIC_OWNER_GROUP_PRODUCTS_PAGE,
            action="product_pick",
            candidate_source=CANDIDATE_SOURCE,
            extra={
                "button_id": get_button_provenance(ctx) or "-",
                "selected_index": index,
            },
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — numeric ownership telemetry is optional
        pass
    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args={
            "product": forced,
            "forced_product": forced,
            "source": DECISION_SOURCE,
            "candidate_source": CANDIDATE_SOURCE,
            "list_index": index,
            "await_quantity": True,
            "selection_context_patch": {
                "selected_product_id": str(forced.get("id") or forced.get("external_id") or ""),
                "selected_variant_id": str(forced.get("variant_id") or ""),
                "selection_context_turn": None,
            },
        },
        reason=f"catalog navigation product pick #{index}",
        confidence=0.95,
    )


__all__ = [
    "CANDIDATE_SOURCE",
    "DECISION_SOURCE",
    "build_rich_forced_product",
    "extract_group_product_pick_index",
    "get_presented_group_products",
    "has_active_group_products_context",
    "is_group_product_pick_message",
    "try_catalog_navigation_product_pick_decision",
]
