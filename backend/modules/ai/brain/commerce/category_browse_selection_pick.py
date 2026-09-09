"""Category browse picks and named product links from presented catalog identity."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ..decision.actions import ACTION_SEARCH_PRODUCTS
from ..types import BrainContext, Decision
from .link_intent import (
    LinkIntentType,
    extract_named_product_link_subject,
    resolve_inbound_link_intent,
)
from .selection_context import (
    _extract_name_pick,
    _normalize_ar,
    _presentation_identity_patch_from_product,
    _product_is_checkout_eligible,
    _product_key,
    _presented_identity_key,
    _resolve_unique_presented_identity,
    get_presented_products,
    normalize_presented_product,
)

logger = logging.getLogger("nahla.brain.category_browse_selection_pick")


def _catalog_candidates_for_category_pick(ctx: BrainContext) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    facts = getattr(ctx, "facts", None)
    for source in (
        list(getattr(facts, "discovery_products", None) or []),
        list(getattr(facts, "top_products", None) or []),
        list(getattr(ctx.state, "last_search_candidates", None) or []),
        list(getattr(ctx.state, "last_presented_products", None) or []),
    ):
        for row in source:
            if isinstance(row, dict):
                rows.append(row)
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = _product_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _match_presented_products_for_category_pick(
    name_pick: str,
    norm: str,
    products: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not name_pick or not products:
        return []
    try:
        from .commerce_browse_category_guard import filter_products_for_browse_turn  # noqa: PLC0415

        scoped = filter_products_for_browse_turn(
            message=norm or name_pick,
            query=name_pick,
            products=list(products),
        )
        if scoped:
            return [
                product
                for product in scoped
                if isinstance(product, dict) and _product_is_checkout_eligible(product)
            ]
    except Exception:  # noqa: BLE001  # noqa: silent-ok — category pick filter is best-effort
        logger.exception("[CATEGORY_BROWSE_PICK] category_pick_filter_failed")
    return []


def try_named_product_link_decision(ctx: BrainContext) -> Optional[Decision]:
    """Ground named product link asks on last presented catalog identity."""
    try:
        if resolve_inbound_link_intent(ctx.message or "") != LinkIntentType.PRODUCT_URL:
            return None
        subject = _presented_identity_key(
            extract_named_product_link_subject(ctx.message or "")
        )
        if not subject:
            return None
        presented = get_presented_products(ctx.state)
        identity_product = _resolve_unique_presented_identity(subject, presented)
        if identity_product is None:
            return None
        product_title = str(
            identity_product.get("title")
            or identity_product.get("display_label")
            or ""
        ).strip()
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": product_title or subject,
                "source": "selection_context_named_product_link",
                "products": [identity_product],
                "presentation_identity_grounded": True,
                "selection_context_patch": _presentation_identity_patch_from_product(
                    identity_product
                ),
            },
            reason="selection_context_named_product_link",
            confidence=0.91,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — named product link must not block decide
        logger.exception("[CATEGORY_BROWSE_PICK] named_product_link_failed")
        return None


def try_category_browse_pick_decision(ctx: BrainContext) -> Optional[Decision]:
    """Present scoped catalog products for category browse without checkout binding."""
    from .commerce_browse_category_guard import (  # noqa: PLC0415
        extract_browse_category_scope,
        is_plural_category_scope_token,
    )

    norm = _normalize_ar(ctx.message or "")
    name_pick = _extract_name_pick(norm)
    if not name_pick:
        return None
    if not extract_browse_category_scope(norm, name_pick):
        return None
    if not is_plural_category_scope_token(name_pick):
        return None
    products = _match_presented_products_for_category_pick(
        name_pick,
        norm,
        _catalog_candidates_for_category_pick(ctx),
    )
    if not products:
        return None
    logger.info(
        "[CATEGORY_BROWSE_PICK] tenant=%s kind=category_browse_pick count=%d preview=%r",
        getattr(ctx, "tenant_id", None),
        len(products),
        (ctx.message or "")[:60],
    )
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={
            "query": name_pick,
            "source": "selection_context_category_browse_pick",
            "products": list(products),
            "presentation_identity_grounded": True,
            "selection_context_patch": {
                "last_presented_products": [
                    normalize_presented_product(product, list_index=i)
                    for i, product in enumerate(products, start=1)
                ],
                "selection_context_turn": int(getattr(ctx.state, "turn", 0) or 0),
            },
        },
        reason="selection_context_category_browse_pick",
        confidence=0.9,
    )


def selection_context_idle_extension(ctx: BrainContext) -> Optional[Decision]:
    """Fallback category browse when selection context does not bind checkout."""
    return try_category_browse_pick_decision(ctx)


def drive_selection_context_decision(
    ctx: BrainContext,
    base_decision,
) -> Optional[Decision]:
    """Augment base selection-context routing with link and category browse picks."""
    from .selection_context import (  # noqa: PLC0415
        has_active_selection_context,
        is_selection_followup_message,
    )

    link_decision = try_named_product_link_decision(ctx)
    if link_decision is not None:
        return link_decision
    if not has_active_selection_context(ctx.state):
        return selection_context_idle_extension(ctx)
    if not is_selection_followup_message(ctx.message or ""):
        base_result = base_decision(ctx)
        if base_result is not None:
            return base_result
        return selection_context_idle_extension(ctx)
    category_decision = try_category_browse_pick_decision(ctx)
    if category_decision is not None:
        return category_decision
    base_result = base_decision(ctx)
    if base_result is not None:
        return base_result
    return selection_context_idle_extension(ctx)


__all__ = [
    "drive_selection_context_decision",
    "selection_context_idle_extension",
    "try_category_browse_pick_decision",
    "try_named_product_link_decision",
]
