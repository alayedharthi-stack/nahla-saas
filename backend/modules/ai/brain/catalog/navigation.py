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
        if get_presented_collections(state) or has_active_collection_navigation_context(state):
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
                        "catalog_navigation_source": "groups",
                    },
                },
            )
        return None

    if (
        has_active_collection_navigation_context(state)
        and get_presented_collections(state)
    ):
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
                        "navigation_state_patch": {
                            "selected_collection": resolution.group_id or resolution.group_slug,
                            "current_catalog_group": {
                                "group_id": resolution.group_id,
                                "group_slug": resolution.group_slug,
                                "group_name": resolution.group_name,
                            },
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
