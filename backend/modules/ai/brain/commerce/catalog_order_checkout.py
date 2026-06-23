"""
commerce/catalog_order_checkout.py
──────────────────────────────────
Phase 1 enforce: WhatsApp native catalog order → continue checkout.

Operational only. This module never writes customer-facing text and never
routes to staff/contact surfaces. It only turns a current WhatsApp
``type=order`` payload into the existing checkout action.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
)
from modules.ai.brain.types import BrainContext, Decision

_FLAG = "WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED"
_FALSY = frozenset({"0", "false", "no", "off"})

_BROWSE_OR_LISTING_ACTIONS = frozenset({
    ACTION_CATALOG_NAVIGATE,
    ACTION_SEARCH_PRODUCTS,
    ACTION_CLARIFY,
    ACTION_NARROW,
    ACTION_LLM_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
})


def catalog_order_continue_checkout_enabled() -> bool:
    """Feature flag for the limited Phase 1 enforce."""
    return os.getenv(_FLAG, "true").strip().lower() not in _FALSY


def _inbound_metadata(ctx: BrainContext) -> Dict[str, Any]:
    profile = getattr(ctx, "profile", None)
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata") or {}
    return dict(meta) if isinstance(meta, dict) else {}


def is_current_catalog_order_submitted(ctx: BrainContext) -> bool:
    """True only for the current inbound WhatsApp native catalog order event."""
    meta = _inbound_metadata(ctx)
    if meta.get("source_type") != "catalog_order":
        return False
    items = meta.get("product_items") or []
    return isinstance(items, list) and bool(items)


def _product_from_state(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    state = getattr(ctx, "state", None)
    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and focus:
        product = dict(focus)
        product.setdefault("from_catalog_order", True)
        product.setdefault("from_native_catalog_order", True)
        return product

    prep = getattr(state, "order_prep", None)
    line_items = list(getattr(prep, "line_items", None) or getattr(state, "cart_items", None) or [])
    first = next((li for li in line_items if isinstance(li, dict)), None)
    if not first:
        return None
    retailer_id = str(first.get("product_retailer_id") or "").strip()
    return {
        "id": first.get("product_id") or retailer_id or "catalog_order",
        "external_id": retailer_id,
        "title": first.get("product_name") or first.get("title") or retailer_id,
        "price": first.get("unit_price") or first.get("price"),
        "currency": first.get("currency") or "",
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "line_items_count": len(line_items),
    }


def maybe_enforce_catalog_order_continue_checkout(
    ctx: BrainContext,
    decision: Decision,
) -> Decision:
    """
    Force current native catalog order events into checkout continuation.

    This prevents regressions where a submitted catalog order drifts back into
    product browse/listing ("وش المتوفر؟", sections, catalog replay) even
    though the customer already selected a concrete product in WhatsApp.
    """
    if not catalog_order_continue_checkout_enabled():
        return decision
    if not is_current_catalog_order_submitted(ctx):
        return decision

    product = _product_from_state(ctx)
    if not product:
        return decision

    args = dict(decision.args or {})
    args.update({
        "product": product,
        "forced_product": product,
        "source": "catalog_order_submitted",
        "catalog_order_submitted": True,
        "continue_checkout": True,
        "native_catalog_order": {
            "event_type": "catalog_order_submitted",
            "source": "whatsapp_catalog",
            "phone_source": "whatsapp",
            "retailer_id": product.get("external_id") or "",
            "line_items_count": product.get("line_items_count") or 1,
        },
    })

    if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
        return Decision(
            action=decision.action,
            args=args,
            reason=decision.reason or "catalog_order_submitted already in checkout",
            confidence=max(decision.confidence, 0.98),
        )

    if decision.action not in _BROWSE_OR_LISTING_ACTIONS:
        # Do not hijack unrelated operational owners; only prevent browse/listing drift.
        return decision

    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args=args,
        reason="catalog_order_submitted → continue_checkout",
        confidence=1.0,
    )


__all__ = [
    "catalog_order_continue_checkout_enabled",
    "is_current_catalog_order_submitted",
    "maybe_enforce_catalog_order_continue_checkout",
]
