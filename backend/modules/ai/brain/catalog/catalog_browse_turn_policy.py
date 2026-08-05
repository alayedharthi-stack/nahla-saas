"""
catalog/catalog_browse_turn_policy.py
─────────────────────────────────────
Platform-wide policy: catalog browse/discovery turns suspend stale checkout
and fulfillment-lock effects for the current turn only.

Operational — intent/state evidence only; merchant differences come from DB
(ProductGroup, catalog_match, ProductRanking), not hardcoded product names.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.catalog.browse_turn_policy")

_BROWSE_DISCOVERY_ENTRY_TYPES = frozenset({
    "global_browse",
    "category_browse",
    "top_products",
    "show_more",
})


def is_catalog_browse_message(message: str, *, intent_name: str = "") -> bool:
    """Return True when message/intent evidence indicates catalog browse, not checkout."""
    msg = message or ""
    intent = str(intent_name or "").strip()

    if intent == "product_visual_request":
        return True

    try:
        from ..commerce.product_visual import is_product_visual_request  # noqa: PLC0415

        if is_product_visual_request(msg):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] product_visual_probe_failed")

    try:
        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            explicit_broad_browse_requested,
            global_availability_browse_requested,
            global_catalog_browse_requested,
        )

        if (
            global_availability_browse_requested(msg)
            or global_catalog_browse_requested(msg)
            or explicit_broad_browse_requested(msg)
        ):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] breadth_policy_probe_failed")

    try:
        from ..discovery.entry import _is_top_seller_request  # noqa: PLC0415

        if _is_top_seller_request(msg):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] top_seller_probe_failed")

    try:
        from ..product_discovery_gate import (  # noqa: PLC0415
            extract_types_overview_query,
            has_types_overview_ask,
            is_generic_category_noun,
        )
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            extract_browse_category_scope,
            is_generic_category_browse,
        )

        if has_types_overview_ask(msg):
            subject = extract_types_overview_query(msg)
            if subject and is_generic_category_noun(subject):
                return True

        scope = extract_browse_category_scope(msg, "")
        if scope and is_generic_category_browse(msg, scope):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] category_browse_probe_failed")

    return False


def is_catalog_browse_turn(
    message: str,
    *,
    intent_name: str = "",
    ctx: Any = None,
) -> bool:
    """Browse turn from message evidence and/or matched discovery entry (no suppression)."""
    if is_catalog_browse_message(message, intent_name=intent_name):
        return True

    if ctx is None:
        return False

    try:
        from ..discovery.entry import _classify_discovery_entry  # noqa: PLC0415

        entry = _classify_discovery_entry(ctx)
        if entry.matched and entry.entry_type in _BROWSE_DISCOVERY_ENTRY_TYPES:
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] discovery_entry_probe_failed")

    return False


def is_fresh_start_order_turn(message: str) -> bool:
    """Bare purchase-intent opener (e.g. «ابي اطلب») — fresh order, not checkout resume."""
    try:
        from ..commerce.start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415

        return is_bare_start_order_phrase(message or "")
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] fresh_start_order_probe_failed")
        return False


def should_suspend_stale_checkout_for_turn(
    message: str,
    *,
    intent_name: str = "",
    ctx: Any = None,
) -> bool:
    """True when browse or bare start-order must isolate stale checkout for this turn."""
    if ctx is not None:
        try:
            from ..turn.ownership import (  # noqa: PLC0415
                FALLBACK_STALE_CHECKOUT_SUSPEND,
                ownership_forbids_fallback,
            )

            if ownership_forbids_fallback(ctx, FALLBACK_STALE_CHECKOUT_SUSPEND):
                return False
        except Exception:  # noqa: BLE001
            logger.exception("[CATALOG_BROWSE_TURN] ownership_suspend_probe_failed")

    try:
        from ..turn.ownership import has_explicit_catalog_browse_intent  # noqa: PLC0415

        if ctx is not None and has_explicit_catalog_browse_intent(
            ctx,
            message=message,
            intent_name=intent_name,
        ):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_BROWSE_TURN] explicit_browse_probe_failed")

    if is_catalog_browse_message(message, intent_name=intent_name):
        return True
    return is_fresh_start_order_turn(message)


def _has_stale_checkout_state(state: Any) -> bool:
    if state is None:
        return False
    op = getattr(state, "order_prep", None)
    if getattr(state, "current_product_focus", None):
        return True
    if op is None:
        return False
    if str(getattr(op, "product_id", "") or "").strip():
        return True
    if list(getattr(op, "missing_fields", None) or []):
        return True
    if str(getattr(state, "draft_order_id", "") or "").strip():
        return True
    return False


def maybe_suspend_stale_checkout_for_turn(
    ctx: Any,
    *,
    message: str = "",
    intent_name: str = "",
) -> bool:
    """
    Clear stale checkout when browse or fresh start-order owns the turn.

    Platform-wide operational isolation — must run before ``decide()``.
    """
    msg = message or getattr(ctx, "message", "") or ""
    intent = intent_name or str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    state = getattr(ctx, "state", None)
    if not should_suspend_stale_checkout_for_turn(msg, intent_name=intent, ctx=ctx):
        return False
    if not _has_stale_checkout_state(state):
        return False
    try:
        from ..commerce.conversation_context_reset import clear_active_order_context  # noqa: PLC0415

        reason = (
            "fresh_start_order_isolation"
            if is_fresh_start_order_turn(msg)
            else "catalog_browse_turn_isolation"
        )
        clear_active_order_context(state, reason=reason)
        logger.info(
            "[CATALOG_BROWSE_TURN] suspended stale checkout tenant=%s reason=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            reason,
            msg[:80],
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_TURN] suspend_stale_checkout_failed tenant=%s",
            getattr(ctx, "tenant_id", None),
        )
        return False


def stamp_catalog_browse_scope_for_turn(
    ctx: Any,
    *,
    query: str = "",
) -> Optional[Any]:
    """Resolve merchant group scope early and stamp commerce session for this turn."""
    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    state = getattr(ctx, "state", None)
    if db is None or tenant_id is None or state is None:
        return None

    try:
        from .catalog_browse_scope_resolver import (  # noqa: PLC0415
            active_catalog_group_slug_from_state,
            resolve_browse_scope,
            stamp_catalog_group_session,
        )
        from ..commerce.commerce_browse_category_guard import active_category_from_state  # noqa: PLC0415

        resolution = resolve_browse_scope(
            db,
            int(tenant_id),
            getattr(ctx, "message", "") or "",
            str(query or ""),
            active_group_slug=active_catalog_group_slug_from_state(state),
            active_category=active_category_from_state(state),
        )
        if resolution.matched:
            stamp_catalog_group_session(state, resolution)
        return resolution
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_BROWSE_TURN] early_browse_scope_failed tenant=%s",
            tenant_id,
        )
        return None


__all__ = [
    "is_catalog_browse_message",
    "is_catalog_browse_turn",
    "is_fresh_start_order_turn",
    "maybe_suspend_stale_checkout_for_turn",
    "should_suspend_stale_checkout_for_turn",
    "stamp_catalog_browse_scope_for_turn",
]
