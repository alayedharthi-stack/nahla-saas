"""
catalog/numeric_ownership.py
────────────────────────────
P1 numeric ownership — single source of truth, hard guards, telemetry.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from ..decision.actions import ACTION_CLARIFY
from ..types import BrainContext, Decision
from .collections_pagination import has_active_collections_browse_context
from .product_pick import (
    extract_group_product_pick_index,
    get_presented_group_products,
    has_active_group_products_context,
    is_group_product_pick_message,
)

logger = logging.getLogger("nahla.brain.catalog.numeric_ownership")

NUMERIC_OWNER_COLLECTIONS_PAGE = "collections_page"
NUMERIC_OWNER_GROUP_PRODUCTS_PAGE = "group_products_page"
NUMERIC_OWNER_SEARCH_LIST = "search_list"
NUMERIC_OWNER_ORDER_OPTIONS = "order_options"
NUMERIC_OWNER_UNKNOWN = "unknown"

_GROUP_PRODUCTS_SOURCE = "group_products"


def is_group_products_navigation_source(state: Any) -> bool:
    return str(getattr(state, "catalog_navigation_source", "") or "").strip() == _GROUP_PRODUCTS_SOURCE


def group_products_candidate_list(state: Any) -> List[Dict[str, Any]]:
    return [dict(row) for row in get_presented_group_products(state) if isinstance(row, dict)]


def sync_group_products_single_source(state: Any) -> List[Dict[str, Any]]:
    """Align search/selection lists with Navigator group-product page."""
    page = group_products_candidate_list(state)
    state.last_presented_group_products = list(page)
    state.last_presented_products = list(page)
    state.last_search_candidates = list(page)
    return page


def get_button_provenance(ctx: BrainContext) -> str:
    meta = dict(getattr(ctx, "profile", None) or {}).get("inbound_metadata") or {}
    for key in ("button_id", "wa_button_id", "button_provenance"):
        val = str(meta.get(key) or "").strip()
        if val:
            return val
    return ""


def resolve_numeric_owner(ctx: BrainContext, *, intent_name: str = "") -> str:
    state = ctx.state
    msg = (ctx.message or "").strip()
    pending_opts = list(getattr(state, "pending_option_groups", None) or [])
    if (
        msg.isdigit()
        and pending_opts
        and getattr(state, "current_product_focus", None)
    ):
        return NUMERIC_OWNER_ORDER_OPTIONS
    if is_group_products_navigation_source(state) or has_active_group_products_context(state):
        return NUMERIC_OWNER_GROUP_PRODUCTS_PAGE
    if has_active_collections_browse_context(state):
        return NUMERIC_OWNER_COLLECTIONS_PAGE
    if list(getattr(state, "last_search_candidates", None) or []):
        return NUMERIC_OWNER_SEARCH_LIST
    if intent_name == "pick_list_item" and list(getattr(state, "last_recommended_products", None) or []):
        return NUMERIC_OWNER_SEARCH_LIST
    return NUMERIC_OWNER_UNKNOWN


def log_numeric_ownership(
    ctx: BrainContext,
    *,
    numeric_owner: str,
    action: str = "",
    intent_name: str = "",
    candidate_source: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    button_id = get_button_provenance(ctx)
    payload = {
        "tenant_id": getattr(ctx, "tenant_id", None),
        "numeric_owner": numeric_owner or NUMERIC_OWNER_UNKNOWN,
        "action": action or "-",
        "button_id": button_id or "-",
        "candidate_source": candidate_source or "-",
        "catalog_navigation_source": str(
            getattr(getattr(ctx, "state", None), "catalog_navigation_source", "") or ""
        ),
        "message_preview": (ctx.message or "")[:40],
    }
    if extra:
        payload.update(extra)
    logger.info(
        "[NUMERIC_OWNERSHIP] tenant=%s numeric_owner=%s action=%s "
        "button_id=%s candidate_source=%s catalog_navigation_source=%s preview=%r",
        payload["tenant_id"],
        payload["numeric_owner"],
        payload["action"],
        payload["button_id"],
        payload["candidate_source"],
        payload["catalog_navigation_source"],
        payload["message_preview"],
    )


def _is_bare_numeric(message: str) -> bool:
    return bool(re.match(r"^\d+$", (message or "").strip()))


def try_group_products_numeric_guard_decision(ctx: BrainContext) -> Optional[Decision]:
    """
    Block legacy collection interpretation of 1/2/3 while in group_products source.
    Safe clarification when product pick cannot resolve.
    """
    from .navigator_exit import navigator_should_yield_to_order_flow  # noqa: PLC0415

    if navigator_should_yield_to_order_flow(ctx.state):
        return None
    if not is_group_products_navigation_source(ctx.state):
        return None
    msg = ctx.message or ""
    if not (is_group_product_pick_message(msg) or _is_bare_numeric(msg)):
        return None

    if has_active_group_products_context(ctx.state):
        index = extract_group_product_pick_index(msg)
        presented = get_presented_group_products(ctx.state)
        if index is not None and 1 <= index <= len(presented):
            return None

    log_numeric_ownership(
        ctx,
        numeric_owner=NUMERIC_OWNER_GROUP_PRODUCTS_PAGE,
        action="blocked_legacy_collection_pick",
        intent_name=getattr(getattr(ctx, "intent", None), "name", "") or "",
        candidate_source="catalog_navigation_group_products",
        extra={
            "presented_count": len(get_presented_group_products(ctx.state)),
        },
    )
    return Decision(
        action=ACTION_CLARIFY,
        args={
            "question": (
                "ما قدرت أحدد المنتج من القائمة المعروضة. "
                "اكتب رقم المنتج من نفس القائمة أو اسمه."
            ),
            "topic": "catalog_navigation_group_products_pick",
            "source": "group_products_numeric_guard",
        },
        reason="group_products source — block legacy collection numeric pick",
        confidence=0.88,
    )


__all__ = [
    "NUMERIC_OWNER_COLLECTIONS_PAGE",
    "NUMERIC_OWNER_GROUP_PRODUCTS_PAGE",
    "NUMERIC_OWNER_ORDER_OPTIONS",
    "NUMERIC_OWNER_SEARCH_LIST",
    "NUMERIC_OWNER_UNKNOWN",
    "get_button_provenance",
    "group_products_candidate_list",
    "is_group_products_navigation_source",
    "log_numeric_ownership",
    "resolve_numeric_owner",
    "sync_group_products_single_source",
    "try_group_products_numeric_guard_decision",
]
