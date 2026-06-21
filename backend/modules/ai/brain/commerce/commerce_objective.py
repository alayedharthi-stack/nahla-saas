"""
commerce/commerce_objective.py
──────────────────────────────
Platform-wide commerce objective persistence — independent from stage.

Objectives survive clarification turns and media messages; they reset only
on explicit topic shift or higher-priority operational funnel evidence.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Optional

from ..discovery.entry import (
    CATEGORY_BROWSE,
    GLOBAL_BROWSE,
    PRODUCT_SPECIFIC,
    SHOW_MORE,
    START_ORDER_BARE,
    TOP_PRODUCTS,
)
from ..state.stages import (
    STAGE_CHECKOUT,
    STAGE_COMPLETE,
    STAGE_DECIDING,
    STAGE_ORDERING,
    STAGE_SUPPORT,
)
from ..types import BrainContext

logger = logging.getLogger("nahla.brain.commerce_objective")

COMMERCE_OBJECTIVE_DISCOVERY = "discovery"
COMMERCE_OBJECTIVE_SELECTION = "selection"
COMMERCE_OBJECTIVE_ORDERING = "ordering"
COMMERCE_OBJECTIVE_PAYMENT = "payment"
COMMERCE_OBJECTIVE_TRACKING = "tracking"
COMMERCE_OBJECTIVE_POST_PURCHASE = "post_purchase"
COMMERCE_OBJECTIVE_SUPPORT = "support"

ALL_COMMERCE_OBJECTIVES = (
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_SELECTION,
    COMMERCE_OBJECTIVE_ORDERING,
    COMMERCE_OBJECTIVE_PAYMENT,
    COMMERCE_OBJECTIVE_TRACKING,
    COMMERCE_OBJECTIVE_POST_PURCHASE,
    COMMERCE_OBJECTIVE_SUPPORT,
)


class CommerceObjective(str, Enum):
    """Platform commerce funnel objective — separate from conversation stage."""

    DISCOVERY = COMMERCE_OBJECTIVE_DISCOVERY
    SELECTION = COMMERCE_OBJECTIVE_SELECTION
    ORDERING = COMMERCE_OBJECTIVE_ORDERING
    PAYMENT = COMMERCE_OBJECTIVE_PAYMENT
    TRACKING = COMMERCE_OBJECTIVE_TRACKING
    POST_PURCHASE = COMMERCE_OBJECTIVE_POST_PURCHASE
    SUPPORT = COMMERCE_OBJECTIVE_SUPPORT

    @classmethod
    def from_value(cls, value: str) -> Optional["CommerceObjective"]:
        try:
            return cls(str(value or "").strip().lower())
        except ValueError:
            return None


def is_valid_commerce_objective(value: str) -> bool:
    return str(value or "").strip().lower() in ALL_COMMERCE_OBJECTIVES

_OBJECTIVE_PRIORITY = {
    COMMERCE_OBJECTIVE_DISCOVERY: 1,
    COMMERCE_OBJECTIVE_SELECTION: 2,
    COMMERCE_OBJECTIVE_ORDERING: 3,
    COMMERCE_OBJECTIVE_PAYMENT: 4,
    COMMERCE_OBJECTIVE_TRACKING: 5,
    COMMERCE_OBJECTIVE_POST_PURCHASE: 6,
    COMMERCE_OBJECTIVE_SUPPORT: 7,
}

_PAYMENT_STATUSES = frozenset({
    "awaiting_payment",
    "awaiting_payment_receipt",
    "awaiting_receipt",
    "payment_pending",
    "pending_review",
    "under_review",
})

_TRACKING_STATUSES = frozenset({
    "processing",
    "preparing",
    "ready",
    "shipped",
    "in_transit",
    "out_for_delivery",
    "delivered",
})


def _priority(objective: str) -> int:
    return _OBJECTIVE_PRIORITY.get(str(objective or "").strip().lower(), 0)


def get_commerce_objective(state: Any) -> str:
    if isinstance(state, dict):
        return str(state.get("commerce_objective") or "").strip().lower()
    return str(getattr(state, "commerce_objective", "") or "").strip().lower()


def _stamp_objective(
    state: Any,
    objective: str,
    *,
    reason: str,
    entry_type: str = "",
) -> None:
    obj = str(objective or "").strip().lower()
    if not obj:
        return
    prev = get_commerce_objective(state)
    turn = int(getattr(state, "turn", 0) or 0)
    state.commerce_objective = obj
    state.commerce_objective_turn = turn
    evidence = dict(getattr(state, "commerce_objective_evidence", None) or {})
    evidence.update({
        "reason": reason,
        "entry_type": entry_type,
        "previous": prev,
        "turn": turn,
    })
    state.commerce_objective_evidence = evidence
    logger.info(
        "[COMMERCE_OBJECTIVE] tenant=- objective=%s prev=%s reason=%s entry=%s turn=%d",
        obj,
        prev or "-",
        reason,
        entry_type or "-",
        turn,
    )


def _explicit_topic_shift(ctx: BrainContext) -> bool:
    try:
        from ..commerce.fallback_guard import detect_hard_topic_shift  # noqa: PLC0415

        return bool(
            detect_hard_topic_shift(
                ctx.message or "",
                history=list(getattr(ctx, "history", None) or []),
                state=ctx.state,
            )
        )
    except Exception:  # noqa: BLE001
        return False


def _objective_from_operational_state(ctx: BrainContext) -> Optional[str]:
    state = ctx.state
    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage == STAGE_SUPPORT:
        return COMMERCE_OBJECTIVE_SUPPORT

    op = getattr(state, "order_prep", None)
    status = ""
    if op is not None:
        status = str(getattr(op, "order_status", "") or "").strip().lower()
        if bool(getattr(op, "payment_receipt_received", False)):
            return COMMERCE_OBJECTIVE_TRACKING
        if status in _PAYMENT_STATUSES:
            return COMMERCE_OBJECTIVE_PAYMENT
        if status in _TRACKING_STATUSES:
            return COMMERCE_OBJECTIVE_TRACKING

    if stage in {STAGE_ORDERING, STAGE_DECIDING} or status in {
        "awaiting_address",
        "awaiting_product",
    }:
        return COMMERCE_OBJECTIVE_ORDERING
    if stage == STAGE_CHECKOUT:
        return COMMERCE_OBJECTIVE_PAYMENT
    if stage == STAGE_COMPLETE:
        return COMMERCE_OBJECTIVE_TRACKING
    if getattr(state, "current_product_focus", None) and stage in {
        STAGE_DECIDING,
        "exploring",
    }:
        return COMMERCE_OBJECTIVE_SELECTION
    return None


def _objective_for_discovery_entry(entry_type: str) -> str:
    if entry_type == PRODUCT_SPECIFIC:
        return COMMERCE_OBJECTIVE_SELECTION
    if entry_type in {
        GLOBAL_BROWSE,
        TOP_PRODUCTS,
        CATEGORY_BROWSE,
        SHOW_MORE,
        START_ORDER_BARE,
    }:
        return COMMERCE_OBJECTIVE_DISCOVERY
    return COMMERCE_OBJECTIVE_DISCOVERY


def update_commerce_objective(
    ctx: BrainContext,
    entry: Any = None,
    *,
    force_reset: bool = False,
) -> str:
    """Update persisted commerce objective; return active objective."""
    state = ctx.state
    current = get_commerce_objective(state)

    if force_reset or _explicit_topic_shift(ctx):
        operational = _objective_from_operational_state(ctx)
        if operational:
            _stamp_objective(state, operational, reason="topic_shift_operational")
            return operational
        entry_type = str(getattr(entry, "entry_type", "") or "")
        inferred = _objective_for_discovery_entry(entry_type) if entry_type else ""
        new_obj = inferred or COMMERCE_OBJECTIVE_DISCOVERY
        _stamp_objective(state, new_obj, reason="topic_shift", entry_type=entry_type)
        return new_obj

    operational = _objective_from_operational_state(ctx)
    if operational and _priority(operational) >= _priority(current):
        if operational != current:
            _stamp_objective(state, operational, reason="operational_state")
        return operational

    if entry is not None and bool(getattr(entry, "matched", False)):
        entry_type = str(getattr(entry, "entry_type", "") or "")
        candidate = _objective_for_discovery_entry(entry_type)
        if candidate == COMMERCE_OBJECTIVE_DISCOVERY and current == COMMERCE_OBJECTIVE_DISCOVERY:
            _stamp_objective(
                state,
                COMMERCE_OBJECTIVE_DISCOVERY,
                reason="discovery_reinforced",
                entry_type=entry_type,
            )
            return COMMERCE_OBJECTIVE_DISCOVERY
        if _priority(candidate) >= _priority(current):
            _stamp_objective(
                state,
                candidate,
                reason="discovery_entry",
                entry_type=entry_type,
            )
            return candidate

    if current:
        return current
    _stamp_objective(state, COMMERCE_OBJECTIVE_DISCOVERY, reason="default")
    return COMMERCE_OBJECTIVE_DISCOVERY


def transition_commerce_objective_for_post_purchase(
    state: Any,
    *,
    reason: str = "post_purchase_signal",
    order_reference: str = "",
) -> str:
    """Shift commerce funnel to post-purchase after delivery/review context."""
    prev = get_commerce_objective(state)
    _stamp_objective(
        state,
        COMMERCE_OBJECTIVE_POST_PURCHASE,
        reason=reason,
    )
    if order_reference:
        evidence = dict(getattr(state, "commerce_objective_evidence", None) or {})
        evidence["order_reference"] = str(order_reference).strip()
        try:
            state.commerce_objective_evidence = evidence
        except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence stamp is best-effort
            pass
    logger.info(
        "[COMMERCE_OBJECTIVE] post_purchase_shift prev=%s new=post_purchase reason=%s",
        prev or "-",
        reason,
    )
    return COMMERCE_OBJECTIVE_POST_PURCHASE


def transition_commerce_objective_for_complaint(state: Any) -> str:
    """Shift active commerce funnel to support when complaint/refund fires."""
    prev = get_commerce_objective(state)
    _stamp_objective(
        state,
        COMMERCE_OBJECTIVE_SUPPORT,
        reason="complaint_refund_topic_shift",
    )
    try:
        from ..state.stages import STAGE_SUPPORT  # noqa: PLC0415

        state.stage = STAGE_SUPPORT
    except Exception:  # noqa: BLE001  # noqa: silent-ok — stage stamp is best-effort on duck-typed state
        pass
    logger.info(
        "[COMMERCE_OBJECTIVE] complaint_shift prev=%s new=support",
        prev or "-",
    )
    return COMMERCE_OBJECTIVE_SUPPORT


__all__ = [
    "ALL_COMMERCE_OBJECTIVES",
    "CommerceObjective",
    "COMMERCE_OBJECTIVE_DISCOVERY",
    "COMMERCE_OBJECTIVE_ORDERING",
    "COMMERCE_OBJECTIVE_PAYMENT",
    "COMMERCE_OBJECTIVE_POST_PURCHASE",
    "COMMERCE_OBJECTIVE_SELECTION",
    "COMMERCE_OBJECTIVE_SUPPORT",
    "COMMERCE_OBJECTIVE_TRACKING",
    "get_commerce_objective",
    "is_valid_commerce_objective",
    "transition_commerce_objective_for_complaint",
    "transition_commerce_objective_for_post_purchase",
    "update_commerce_objective",
]
