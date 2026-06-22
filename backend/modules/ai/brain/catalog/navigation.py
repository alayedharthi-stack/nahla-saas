"""
catalog/navigation.py
─────────────────────
CatalogNavigator — deterministic owner for groups navigation turns.

Owns only: browse groups, group selection, group products rendering,
back/switch group, and no-groups top-products fallback.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from ..decision.actions import ACTION_CATALOG_NAVIGATE
from ..types import BrainContext, Decision
from .navigation_signals import (
    HIGH_BROWSE_THRESHOLD,
    evaluate_catalog_navigation_signals,
    is_collections_start_over_request,
    is_navigation_more_request,
)
from .collections_pagination import (
    COLLECTIONS_BUTTON_PAGE_SIZE,
    get_collections_pool,
    has_active_collections_browse_context,
)
from .product_pick import (
    has_active_group_products_context,
    is_group_product_pick_message,
)

logger = logging.getLogger("nahla.brain.catalog.navigation")

TURN_OWNER = "catalog_navigation"

PATH_GROUPS = "catalog_navigation_groups"
PATH_GROUP_PRODUCTS = "catalog_navigation_group_products"
PATH_TOP_FALLBACK = "catalog_navigation_top_products_fallback"

NAVIGATOR_PROTECTED_PATHS = frozenset({
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    PATH_TOP_FALLBACK,
})

STEP_SHOW_GROUPS = "show_groups"
STEP_SHOW_GROUP_PRODUCTS = "show_group_products"
STEP_TOP_FALLBACK = "top_products_fallback"

OWNER_STEP_BROWSE_GROUPS = "browse_groups"
OWNER_STEP_GROUP_SELECTION = "group_selection"
OWNER_STEP_GROUP_PRODUCTS = "group_products"
OWNER_STEP_TOP_FALLBACK = "top_products_fallback"


def is_navigator_protected_path(path: str) -> bool:
    return str(path or "").strip() in NAVIGATOR_PROTECTED_PATHS


def is_navigator_owned_result(data: Optional[Dict[str, Any]]) -> bool:
    payload = data or {}
    return (
        str(payload.get("turn_owner") or "").strip() == TURN_OWNER
        and bool(payload.get("owner_locked"))
    )


def owner_reply_hash(reply: str) -> str:
    return hashlib.sha256(str(reply or "").encode("utf-8")).hexdigest()[:16]


def _owned_decision(
    *,
    navigator_step: str,
    owner_step: str,
    chosen_path: str,
    reason: str,
    confidence: float,
    extra_args: Optional[Dict[str, Any]] = None,
) -> Decision:
    args: Dict[str, Any] = {
        "navigator_step": navigator_step,
        "turn_owner": TURN_OWNER,
        "owner_locked": True,
        "owner_step": owner_step,
        "chosen_path": chosen_path,
        "owner_replaced": False,
        "navigation_state_patch": {},
    }
    if extra_args:
        args.update(extra_args)
    return Decision(
        action=ACTION_CATALOG_NAVIGATE,
        args=args,
        reason=reason,
        confidence=confidence,
    )


def _log_navigator_event(
    ctx: BrainContext,
    *,
    navigator_owner: bool,
    owner_step: str = "",
    chosen_path: str = "",
    owner_exit_reason: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload = {
        "navigator_owner": navigator_owner,
        "owner_locked": navigator_owner,
        "owner_replaced": False,
        "owner_step": owner_step or "-",
        "chosen_path": chosen_path or "-",
        "owner_exit_reason": owner_exit_reason or "-",
    }
    if extra:
        payload.update(extra)
    logger.info(
        "[CATALOG_NAVIGATOR] tenant=%s owner=%s step=%s path=%s exit=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        navigator_owner,
        payload["owner_step"],
        payload["chosen_path"],
        payload["owner_exit_reason"],
        (ctx.message or "")[:60],
    )


def _load_catalog_groups(ctx: BrainContext) -> List[Dict[str, Any]]:
    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    if db is None or tenant_id is None:
        return []
    try:
        from .catalog_browse_scope_resolver import load_merchant_catalog_groups  # noqa: PLC0415

        return list(load_merchant_catalog_groups(db, int(tenant_id)) or [])
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional DB group load
        logger.exception("[CATALOG_NAVIGATOR] group_load_failed tenant=%s", tenant_id)
        return []


def _resolve_direct_group_name(
    message: str,
    collections: List[Dict[str, Any]],
) -> Optional[Any]:
    from ..commerce.collection_navigation import (  # noqa: PLC0415
        CollectionResolution,
        _collection_id,
        _collection_label,
        _match_collection_by_name,
    )

    hits = _match_collection_by_name(message, collections)
    if len(hits) != 1:
        return None
    row = hits[0]
    return CollectionResolution(
        group_id=_collection_id(row),
        group_slug=str(row.get("group_id") or row.get("slug") or "").strip(),
        group_name=_collection_label(row),
        list_index=int(row.get("list_index") or 0),
    )


def _looks_like_group_name_pick(message: str, collections: List[Dict[str, Any]]) -> bool:
    from ..commerce.collection_navigation import _normalize_ar  # noqa: PLC0415

    norm = _normalize_ar(message or "")
    if not norm or len(norm.split()) > 4:
        return False
    if norm.isdigit():
        return False
    return bool(collections)


def try_catalog_navigation_decision(ctx: BrainContext) -> Optional[Decision]:
    """Resolve owned catalog navigation turns before discovery/search/product_media."""
    if not getattr(getattr(ctx, "facts", None), "has_products", False):
        return None

    from .navigator_exit import navigator_should_yield_to_order_flow  # noqa: PLC0415

    if navigator_should_yield_to_order_flow(ctx.state):
        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason="order_flow_yield",
        )
        return None

    signals = evaluate_catalog_navigation_signals(ctx)
    msg = ctx.message or ""

    if signals.hard_blocked:
        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason=signals.block_reason,
            extra={"signals": signals.evidence},
        )
        return None

    from ..commerce.collection_navigation import (  # noqa: PLC0415
        get_presented_collections,
        has_active_collection_navigation_context,
        is_back_to_collections_request,
        is_collection_pick_message,
        is_switch_collection_request,
        resolve_collection_pick,
    )

    state = ctx.state

    if is_back_to_collections_request(msg) or is_switch_collection_request(msg):
        if (
            get_presented_collections(state)
            or has_active_collection_navigation_context(state)
            or has_active_group_products_context(state)
        ):
            _log_navigator_event(
                ctx,
                navigator_owner=True,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
            )
            return _owned_decision(
                navigator_step=STEP_SHOW_GROUPS,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
                reason="catalog navigation — back/switch to group list",
                confidence=0.93,
                extra_args={
                    "navigation_state_patch": {
                        "selected_collection": "",
                        "current_catalog_group": None,
                        "collections_pool": [],
                        "collections_offset": 0,
                        "collections_page_size": COLLECTIONS_BUTTON_PAGE_SIZE,
                        "collections_next_available": False,
                        "group_products_pool": [],
                        "group_products_offset": 0,
                        "group_products_page_size": 0,
                        "next_page_available": False,
                        "last_presented_group_products": [],
                        "catalog_navigation_source": "groups",
                    },
                },
            )
        return None

    if has_active_collections_browse_context(state):
        if is_collections_start_over_request(msg):
            _log_navigator_event(
                ctx,
                navigator_owner=True,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
                owner_exit_reason="collections_start_over",
            )
            return _owned_decision(
                navigator_step=STEP_SHOW_GROUPS,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
                reason="catalog navigation — collections back to start",
                confidence=0.92,
                extra_args={
                    "collections_offset": 0,
                    "reuse_collections_pool": True,
                    "collections_pool": get_collections_pool(state),
                    "navigation_state_patch": {
                        "selected_collection": "",
                        "current_catalog_group": None,
                        "catalog_navigation_source": "groups",
                    },
                },
            )

        if is_navigation_more_request(msg):
            pool = get_collections_pool(state)
            offset = int(getattr(state, "collections_offset", 0) or 0)
            page_size = COLLECTIONS_BUTTON_PAGE_SIZE
            next_offset = offset + page_size
            if not pool or next_offset >= len(pool):
                last_offset = max(0, len(pool) - page_size) if pool else 0
                _log_navigator_event(
                    ctx,
                    navigator_owner=True,
                    owner_step=OWNER_STEP_BROWSE_GROUPS,
                    chosen_path=PATH_GROUPS,
                    owner_exit_reason="collections_exhausted",
                )
                return _owned_decision(
                    navigator_step=STEP_SHOW_GROUPS,
                    owner_step=OWNER_STEP_BROWSE_GROUPS,
                    chosen_path=PATH_GROUPS,
                    reason="catalog navigation — collections exhausted",
                    confidence=0.9,
                    extra_args={
                        "collections_offset": last_offset,
                        "collections_pool": pool,
                        "reuse_collections_pool": True,
                        "collections_at_end": True,
                        "navigation_state_patch": {
                            "selected_collection": "",
                            "current_catalog_group": None,
                            "catalog_navigation_source": "groups",
                        },
                    },
                )
            _log_navigator_event(
                ctx,
                navigator_owner=True,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
                extra={"collections_offset": next_offset, "pool_size": len(pool)},
            )
            return _owned_decision(
                navigator_step=STEP_SHOW_GROUPS,
                owner_step=OWNER_STEP_BROWSE_GROUPS,
                chosen_path=PATH_GROUPS,
                reason="catalog navigation — next collections page",
                confidence=0.92,
                extra_args={
                    "collections_offset": next_offset,
                    "collections_pool": pool,
                    "reuse_collections_pool": True,
                    "navigation_state_patch": {
                        "selected_collection": "",
                        "current_catalog_group": None,
                        "catalog_navigation_source": "groups",
                    },
                },
            )

        if is_collection_pick_message(msg) or _looks_like_group_name_pick(
            msg, get_presented_collections(state),
        ):
            resolution = resolve_collection_pick(msg, get_presented_collections(state))
            if resolution is None:
                resolution = _resolve_direct_group_name(msg, get_collections_pool(state))
            if resolution is not None:
                try:
                    from .numeric_ownership import (  # noqa: PLC0415
                        NUMERIC_OWNER_COLLECTIONS_PAGE,
                        log_numeric_ownership,
                    )

                    log_numeric_ownership(
                        ctx,
                        numeric_owner=NUMERIC_OWNER_COLLECTIONS_PAGE,
                        action="collection_pick",
                        candidate_source="catalog_navigation_collections",
                        extra={"group_name": resolution.group_name},
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry optional
                    pass
                _log_navigator_event(
                    ctx,
                    navigator_owner=True,
                    owner_step=OWNER_STEP_GROUP_SELECTION,
                    chosen_path=PATH_GROUP_PRODUCTS,
                )
                return _owned_decision(
                    navigator_step=STEP_SHOW_GROUP_PRODUCTS,
                    owner_step=OWNER_STEP_GROUP_SELECTION,
                    chosen_path=PATH_GROUP_PRODUCTS,
                    reason=f"catalog navigation — selected group {resolution.group_name!r}",
                    confidence=0.94,
                    extra_args={
                        "catalog_group_id": resolution.group_id,
                        "catalog_group_slug": resolution.group_slug,
                        "query": resolution.group_name,
                        "group_products_offset": 0,
                        "reuse_group_pool": False,
                        "navigation_state_patch": {
                            "selected_collection": resolution.group_id or resolution.group_slug,
                            "current_catalog_group": {
                                "group_id": resolution.group_id,
                                "group_slug": resolution.group_slug,
                                "group_name": resolution.group_name,
                            },
                            "group_products_pool": [],
                            "group_products_offset": 0,
                            "group_products_page_size": 0,
                            "next_page_available": False,
                            "catalog_navigation_source": "group_products",
                        },
                    },
                )

        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason="collections_browse_context_active",
        )
        return None

    if has_active_group_products_context(state):
        from ..commerce.selection_context import get_presented_products  # noqa: PLC0415

        if is_navigation_more_request(msg):
            pool = list(getattr(state, "group_products_pool", None) or [])
            offset = int(getattr(state, "group_products_offset", 0) or 0)
            page_size = int(getattr(state, "group_products_page_size", 0) or 0)
            if page_size <= 0:
                page_size = max(1, len(get_presented_products(state) or []))
            next_offset = offset + page_size
            current_group = getattr(state, "current_catalog_group", None) or {}
            group_name = str(
                current_group.get("group_name")
                or current_group.get("name")
                or getattr(state, "selected_collection", "")
                or ""
            ).strip()
            if not pool or next_offset >= len(pool):
                _log_navigator_event(
                    ctx,
                    navigator_owner=True,
                    owner_step=OWNER_STEP_BROWSE_GROUPS,
                    chosen_path=PATH_GROUPS,
                    owner_exit_reason="group_products_exhausted",
                )
                return _owned_decision(
                    navigator_step=STEP_SHOW_GROUPS,
                    owner_step=OWNER_STEP_BROWSE_GROUPS,
                    chosen_path=PATH_GROUPS,
                    reason="catalog navigation — no more products in group",
                    confidence=0.9,
                    extra_args={
                        "navigation_state_patch": {
                            "selected_collection": "",
                            "current_catalog_group": None,
                            "group_products_pool": [],
                            "group_products_offset": 0,
                            "group_products_page_size": 0,
                            "next_page_available": False,
                            "last_presented_group_products": [],
                            "catalog_navigation_source": "groups",
                        },
                    },
                )
            _log_navigator_event(
                ctx,
                navigator_owner=True,
                owner_step=OWNER_STEP_GROUP_PRODUCTS,
                chosen_path=PATH_GROUP_PRODUCTS,
                extra={"offset": next_offset, "pool_size": len(pool)},
            )
            return _owned_decision(
                navigator_step=STEP_SHOW_GROUP_PRODUCTS,
                owner_step=OWNER_STEP_GROUP_PRODUCTS,
                chosen_path=PATH_GROUP_PRODUCTS,
                reason=f"catalog navigation — next page for group {group_name!r}",
                confidence=0.92,
                extra_args={
                    "catalog_group_id": current_group.get("group_id") or current_group.get("id"),
                    "catalog_group_slug": current_group.get("group_slug") or current_group.get("slug"),
                    "query": group_name,
                    "group_products_offset": next_offset,
                    "group_products_pool": pool,
                    "group_products_page_size": page_size,
                    "reuse_group_pool": True,
                    "navigation_state_patch": {
                        "selected_collection": str(
                            current_group.get("group_id")
                            or current_group.get("group_slug")
                            or getattr(state, "selected_collection", "")
                            or ""
                        ),
                        "current_catalog_group": current_group,
                        "catalog_navigation_source": "group_products",
                    },
                },
            )

        if is_group_product_pick_message(msg):
            _log_navigator_event(
                ctx,
                navigator_owner=False,
                owner_exit_reason="group_product_pick_handoff",
            )
            return None

        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason="group_products_context_active",
        )
        return None

    if (
        has_active_collection_navigation_context(state)
        and get_presented_collections(state)
        and not has_active_group_products_context(state)
    ):
        from ..catalog.numeric_ownership import is_group_products_navigation_source  # noqa: PLC0415

        if is_group_products_navigation_source(state) and is_collection_pick_message(msg):
            _log_navigator_event(
                ctx,
                navigator_owner=False,
                owner_exit_reason="group_products_numeric_guard_deferred",
            )
            return None

        from ..commerce.selection_context import get_presented_products  # noqa: PLC0415

        if (
            get_presented_products(state)
            and str(getattr(state, "selected_collection", "") or "").strip()
            and not is_collection_pick_message(msg)
            and not is_back_to_collections_request(msg)
            and not is_switch_collection_request(msg)
        ):
            _log_navigator_event(
                ctx,
                navigator_owner=False,
                owner_exit_reason="group_products_already_shown",
            )
            return None

        if is_collection_pick_message(msg) or _looks_like_group_name_pick(
            msg, get_presented_collections(state),
        ):
            resolution = resolve_collection_pick(msg, get_presented_collections(state))
            if resolution is None:
                resolution = _resolve_direct_group_name(msg, get_presented_collections(state))
            if resolution is not None:
                try:
                    from .numeric_ownership import (  # noqa: PLC0415
                        NUMERIC_OWNER_COLLECTIONS_PAGE,
                        log_numeric_ownership,
                    )

                    log_numeric_ownership(
                        ctx,
                        numeric_owner=NUMERIC_OWNER_COLLECTIONS_PAGE,
                        action="collection_pick",
                        candidate_source="catalog_navigation_collections",
                        extra={"group_name": resolution.group_name},
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry optional
                    pass
                _log_navigator_event(
                    ctx,
                    navigator_owner=True,
                    owner_step=OWNER_STEP_GROUP_SELECTION,
                    chosen_path=PATH_GROUP_PRODUCTS,
                )
                return _owned_decision(
                    navigator_step=STEP_SHOW_GROUP_PRODUCTS,
                    owner_step=OWNER_STEP_GROUP_SELECTION,
                    chosen_path=PATH_GROUP_PRODUCTS,
                    reason=f"catalog navigation — selected group {resolution.group_name!r}",
                    confidence=0.94,
                    extra_args={
                        "catalog_group_id": resolution.group_id,
                        "catalog_group_slug": resolution.group_slug,
                        "query": resolution.group_name,
                        "group_products_offset": 0,
                        "reuse_group_pool": False,
                        "navigation_state_patch": {
                            "selected_collection": resolution.group_id or resolution.group_slug,
                            "current_catalog_group": {
                                "group_id": resolution.group_id,
                                "group_slug": resolution.group_slug,
                                "group_name": resolution.group_name,
                            },
                            "group_products_pool": [],
                            "group_products_offset": 0,
                            "group_products_page_size": 0,
                            "next_page_available": False,
                            "catalog_navigation_source": "group_products",
                        },
                    },
                )

    if signals.advisory_or_comparison:
        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason=signals.exit_reason or "advisory_or_comparison",
            extra={"signals": signals.evidence},
        )
        return None

    if signals.catalog_browse_score < HIGH_BROWSE_THRESHOLD and not signals.catalog_browse_intent:
        _log_navigator_event(
            ctx,
            navigator_owner=False,
            owner_exit_reason="low_browse_confidence",
            extra={"signals": signals.evidence, "score": signals.catalog_browse_score},
        )
        return None

    groups = _load_catalog_groups(ctx)
    if not groups:
        _log_navigator_event(
            ctx,
            navigator_owner=True,
            owner_step=OWNER_STEP_TOP_FALLBACK,
            chosen_path=PATH_TOP_FALLBACK,
            extra={"navigator_no_groups_fallback": True},
        )
        return _owned_decision(
            navigator_step=STEP_TOP_FALLBACK,
            owner_step=OWNER_STEP_TOP_FALLBACK,
            chosen_path=PATH_TOP_FALLBACK,
            reason="catalog navigation — no groups fallback to top products",
            confidence=0.88,
            extra_args={
                "navigator_no_groups_fallback": True,
                "navigation_state_patch": {
                    "selected_collection": "",
                    "current_catalog_group": None,
                    "catalog_navigation_source": "top_fallback",
                },
            },
        )

    _log_navigator_event(
        ctx,
        navigator_owner=True,
        owner_step=OWNER_STEP_BROWSE_GROUPS,
        chosen_path=PATH_GROUPS,
        extra={"signals": signals.evidence},
    )
    return _owned_decision(
        navigator_step=STEP_SHOW_GROUPS,
        owner_step=OWNER_STEP_BROWSE_GROUPS,
        chosen_path=PATH_GROUPS,
        reason="catalog navigation — browse groups",
        confidence=max(signals.confidence, 0.91),
        extra_args={
            "navigation_state_patch": {
                "selected_collection": "",
                "current_catalog_group": None,
                "collections_pool": [],
                "collections_offset": 0,
                "collections_page_size": COLLECTIONS_BUTTON_PAGE_SIZE,
                "collections_next_available": False,
                "catalog_navigation_source": "groups",
            },
        },
    )


__all__ = [
    "NAVIGATOR_PROTECTED_PATHS",
    "PATH_GROUPS",
    "PATH_GROUP_PRODUCTS",
    "PATH_TOP_FALLBACK",
    "STEP_SHOW_GROUPS",
    "STEP_SHOW_GROUP_PRODUCTS",
    "STEP_TOP_FALLBACK",
    "TURN_OWNER",
    "is_navigator_owned_result",
    "is_navigator_protected_path",
    "owner_reply_hash",
    "try_catalog_navigation_decision",
]
