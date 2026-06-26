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
    message = str(getattr(ctx, "message", "") or "")
    try:
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        return is_catalog_order_inbound(meta, message)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — local fallback below keeps legacy behavior
        pass
    source = str(meta.get("source_type") or "").strip().lower()
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    items = meta.get("product_items") or order.get("product_items") or []
    if source in {"catalog_order", "order"} and isinstance(items, list) and bool(items):
        return True
    if "[طلب كتالوج من العميل]" in message and items:
        return True
    return False


def _salla_external_id_from_line_item(item: Dict[str, Any]) -> str:
    """Store-platform product id only — never WhatsApp ``product_retailer_id`` / SKU."""
    for key in ("salla_product_id", "store_external_id", "store_product_id"):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def _catalog_items_from_metadata(meta: Dict[str, Any]) -> list[Dict[str, Any]]:
    raw = meta.get("product_items")
    if isinstance(raw, list) and raw:
        return [dict(x) for x in raw if isinstance(x, dict)]
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    nested = order.get("product_items")
    if isinstance(nested, list) and nested:
        return [dict(x) for x in nested if isinstance(x, dict)]
    return []


def _product_from_inbound_metadata(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    meta = _inbound_metadata(ctx)
    if not is_current_catalog_order_submitted(ctx):
        return None
    items = _catalog_items_from_metadata(meta)
    if not items:
        return None
    first = items[0]
    count = len(items)
    retailer_id = str(
        first.get("product_retailer_id")
        or first.get("sku")
        or first.get("retailer_id")
        or "",
    ).strip()
    names = meta.get("product_names")
    title = ""
    if isinstance(names, list) and names:
        title = str(names[0] or "").strip()
    if not title:
        title = str(first.get("product_name") or first.get("title") or "").strip()
    try:
        total_price = float(meta.get("total_price")) if meta.get("total_price") is not None else None
    except (TypeError, ValueError):
        total_price = None
    currency = str(meta.get("currency") or first.get("currency") or "").strip()
    product: Dict[str, Any] = {
        "id": retailer_id or "catalog_order",
        "title": title or retailer_id or "catalog_order",
        "price": total_price,
        "currency": currency,
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "line_items_count": count,
        "is_multi_item": count > 1,
        "line_items": items,
    }
    if retailer_id:
        product["product_retailer_id"] = retailer_id
    store_external_id = _salla_external_id_from_line_item(first)
    if store_external_id:
        product["external_id"] = store_external_id
    return product


def _product_from_state(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None)
    line_items = list(
        getattr(prep, "line_items", None) or getattr(state, "cart_items", None) or []
    )
    if line_items:
        first = next((li for li in line_items if isinstance(li, dict)), None) or {}
        count = len(line_items)
        retailer_id = str(first.get("product_retailer_id") or first.get("sku") or "").strip()
        product: Dict[str, Any] = {
            "id": first.get("product_id") or retailer_id or "catalog_order",
            "title": first.get("product_name") or first.get("title") or retailer_id or "",
            "price": prep.catalog_checkout_total if prep is not None else first.get("unit_price"),
            "currency": (
                getattr(prep, "catalog_checkout_currency", None)
                or first.get("currency")
                or ""
            ),
            "from_catalog_order": True,
            "from_native_catalog_order": True,
            "line_items_count": count,
            "is_multi_item": count > 1,
            "line_items": [dict(x) for x in line_items if isinstance(x, dict)],
        }
        if retailer_id:
            product["product_retailer_id"] = retailer_id
        store_external_id = _salla_external_id_from_line_item(first)
        if store_external_id:
            product["external_id"] = store_external_id
        return product

    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and focus:
        product = dict(focus)
        product.setdefault("from_catalog_order", True)
        product.setdefault("from_native_catalog_order", True)
        if product.get("from_native_catalog_order"):
            ext = str(product.get("external_id") or "").strip()
            if ext and not product.get("product_retailer_id"):
                product["product_retailer_id"] = ext
            product.pop("external_id", None)
        return product

    product = _product_from_inbound_metadata(ctx)
    if product:
        return product

    return None


def _catalog_order_continue_args(
    ctx: BrainContext,
    product: Dict[str, Any],
    *,
    reason: str,
) -> Dict[str, Any]:
    return {
        "product": product,
        "forced_product": product,
        "source": "catalog_order_submitted",
        "catalog_order_submitted": True,
        "continue_checkout": True,
        "native_catalog_order": {
            "event_type": "catalog_order_submitted",
            "source": "whatsapp_catalog",
            "phone_source": "whatsapp",
            "retailer_id": product.get("product_retailer_id") or "",
            "line_items_count": product.get("line_items_count") or 1,
        },
        "use_catalog_prices_only": True,
        "skip_product_discovery": True,
    }


def try_catalog_order_continue_decision(ctx: BrainContext) -> Optional[Decision]:
    """Early route: WhatsApp catalog order beats browse/search/discovery."""
    if not catalog_order_continue_checkout_enabled():
        return None
    if not is_current_catalog_order_submitted(ctx):
        return None
    decision = try_active_catalog_checkout_continue_decision(ctx)
    if decision is None:
        return None
    return Decision(
        action=decision.action,
        args=dict(decision.args or {}),
        reason="catalog_order_submitted → continue_checkout",
        confidence=1.0,
    )


def _prep_dict(order_prep: Any) -> Dict[str, Any]:
    if order_prep is None:
        return {}
    if isinstance(order_prep, dict):
        return dict(order_prep)
    if hasattr(order_prep, "to_dict"):
        try:
            return dict(order_prep.to_dict())
        except Exception:  # noqa: BLE001  # noqa: silent-ok — prep to_dict probe must not block checkout detection
            pass
    return dict(getattr(order_prep, "__dict__", {}) or {})


def is_active_catalog_checkout(ctx: BrainContext) -> bool:
    """
    True when a native catalog checkout session is still in progress — including
    follow-up turns after the initial catalog_order event.
    """
    if is_current_catalog_order_submitted(ctx):
        return True
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    prep_d = _prep_dict(prep)
    if is_catalog_line_items_authoritative_from_prep(prep):
        return True
    line_items = list(prep_d.get("line_items") or [])
    if line_items and prep_d.get("catalog_checkout_total") is not None:
        return True
    if prep_d.get("order_flow_v2_active") and prep_d.get("order_flow_v2_trusted_price"):
        return True
    for item in line_items:
        if not isinstance(item, dict):
            continue
        if item.get("from_native_catalog_order") or str(item.get("source") or "") == "whatsapp_native_catalog_order":
            return True
    focus = getattr(state, "current_product_focus", None) if state else None
    if isinstance(focus, dict) and (
        focus.get("from_catalog_order") or focus.get("from_native_catalog_order")
    ):
        if line_items or prep_d.get("catalog_checkout_total") is not None:
            return True
    return False


def try_active_catalog_checkout_continue_decision(ctx: BrainContext) -> Optional[Decision]:
    """Continue checkout from order_prep / active draft — not only current-turn catalog_order."""
    if not catalog_order_continue_checkout_enabled():
        return None
    if not is_active_catalog_checkout(ctx):
        return None
    product = _product_from_state(ctx)
    if not product:
        return None
    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args=_catalog_order_continue_args(
            ctx,
            product,
            reason="active_catalog_checkout → continue_checkout",
        ),
        reason="active_catalog_checkout → continue_checkout",
        confidence=0.98,
    )


def is_catalog_line_items_authoritative_from_prep(order_prep: Any) -> bool:
    """True when native catalog line items must not be cleared or re-prompted."""
    if order_prep is None:
        return False
    if isinstance(order_prep, dict):
        if order_prep.get("catalog_line_items_authoritative"):
            return True
        line_items = order_prep.get("line_items") or []
        if isinstance(line_items, list) and line_items:
            if order_prep.get("catalog_checkout_total") is not None:
                return True
        return False
    if bool(getattr(order_prep, "catalog_line_items_authoritative", False)):
        return True
    line_items = list(getattr(order_prep, "line_items", None) or [])
    if line_items and getattr(order_prep, "catalog_checkout_total", None) is not None:
        return True
    return False


def is_catalog_line_items_authoritative(ctx: BrainContext) -> bool:
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    if is_catalog_line_items_authoritative_from_prep(prep):
        return True
    meta = _inbound_metadata(ctx)
    if str(meta.get("source_type") or "").strip().lower() == "catalog_order":
        items = meta.get("product_items") or []
        return isinstance(items, list) and bool(items)
    return False


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
    if not is_current_catalog_order_submitted(ctx) and not is_active_catalog_checkout(ctx):
        return decision

    product = _product_from_state(ctx)
    if not product:
        return decision

    args = dict(decision.args or {})
    args.update(
        _catalog_order_continue_args(
            ctx,
            product,
            reason="catalog_order_submitted → continue_checkout",
        ),
    )

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
    "is_active_catalog_checkout",
    "is_catalog_line_items_authoritative",
    "is_catalog_line_items_authoritative_from_prep",
    "is_current_catalog_order_submitted",
    "maybe_enforce_catalog_order_continue_checkout",
    "try_active_catalog_checkout_continue_decision",
    "try_catalog_order_continue_decision",
]
