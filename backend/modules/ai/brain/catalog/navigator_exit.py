"""
catalog/navigator_exit.py
─────────────────────────
P0 — release CatalogNavigator ownership when order flow takes over.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..decision.actions import ACTION_PROPOSE_DRAFT_ORDER
from ..types import Decision
from .product_pick import DECISION_SOURCE

logger = logging.getLogger("nahla.brain.catalog.navigator_exit")

EXIT_REASON_ORDER_HANDOFF = "order_handoff"
_ORDER_FLOW_STAGES = frozenset({"ordering", "checkout", "deciding"})


def has_active_order_prep(state: Any) -> bool:
    """True when order_prep carries live order data (not the empty default)."""
    op = getattr(state, "order_prep", None)
    if op is None:
        return False
    if str(getattr(op, "product_id", "") or "").strip():
        return True
    if list(getattr(op, "missing_fields", None) or []):
        return True
    if getattr(op, "awaiting_variant_choice", False):
        return True
    if getattr(op, "awaiting_payment_receipt", False):
        return True
    if getattr(op, "awaiting_option_confirmation", False):
        return True
    return False


def navigator_should_yield_to_order_flow(state: Any) -> bool:
    """Navigator hooks must not own turns that belong to order/checkout."""
    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage in _ORDER_FLOW_STAGES:
        return True
    return has_active_order_prep(state)


def is_catalog_navigation_order_handoff_decision(decision: Decision) -> bool:
    if str(getattr(decision, "action", "") or "") != ACTION_PROPOSE_DRAFT_ORDER:
        return False
    source = str((getattr(decision, "args", None) or {}).get("source") or "").strip()
    return source == DECISION_SOURCE


def clear_navigator_state_for_order_handoff(
    state: Any,
    *,
    tenant_id: Optional[int] = None,
) -> None:
    """Drop browse-only Navigator state after a successful navigator product pick."""
    if state is None:
        return

    had_source = str(getattr(state, "catalog_navigation_source", "") or "").strip()
    had_group = bool(getattr(state, "current_catalog_group", None))
    had_products = bool(list(getattr(state, "last_presented_group_products", None) or []))

    state.catalog_navigation_source = ""
    state.current_catalog_group = None
    state.last_presented_group_products = []
    state.group_products_pool = []
    state.group_products_offset = 0
    state.group_products_page_size = 0
    state.next_page_available = False
    state.collections_pool = []
    state.collections_offset = 0
    state.collections_page_size = 0
    state.collections_next_available = False
    state.selected_collection = ""
    state.last_presented_collections = []

    if had_source == "group_products" or had_products:
        state.last_presented_products = []
        state.last_search_candidates = []

    logger.info(
        "[CATALOG_NAVIGATOR] navigator_exit=true exit_reason=%s tenant=%s "
        "had_source=%r had_group=%s had_group_products=%s",
        EXIT_REASON_ORDER_HANDOFF,
        tenant_id,
        had_source or "-",
        had_group,
        had_products,
    )


__all__ = [
    "EXIT_REASON_ORDER_HANDOFF",
    "clear_navigator_state_for_order_handoff",
    "has_active_order_prep",
    "is_catalog_navigation_order_handoff_decision",
    "navigator_should_yield_to_order_flow",
]
