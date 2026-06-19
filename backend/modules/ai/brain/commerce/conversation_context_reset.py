"""
commerce/conversation_context_reset.py
──────────────────────────────────────
Order conversation context TTL and reset rules.

Do not reset ordering context on every fallback. Close active order context only
when fulfillment completes, the customer cancels, TTL expires, or staff resets.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.conversation_context_reset")

STAGE_BROWSING = "browsing"
STAGE_PRODUCT_SELECTED = "product_selected"
STAGE_ORDER_DRAFT = "order_draft"
STAGE_AWAITING_FULFILLMENT = "awaiting_address_payment"
STAGE_PAID_ORDER = "paid_order"
STAGE_CLOSED = "closed"
STAGE_NONE = "none"

_TTL_HOURS = {
    STAGE_BROWSING: 6,
    STAGE_PRODUCT_SELECTED: 12,
    STAGE_ORDER_DRAFT: 24,
    STAGE_AWAITING_FULFILLMENT: 48,
    STAGE_PAID_ORDER: None,
    STAGE_CLOSED: 0,
    STAGE_NONE: None,
}

_CANCEL_RE = re.compile(
    r"(?:"
    r"الغ(?:ي|اء|ِ|)|ألغ(?:ي|)|الغيت|"
    r"ما\s*اب(?:ي|غى)|ما\s*أب(?:ي|غى)|"
    r"لا\s*اب(?:ي|غى)|لا\s*أب(?:ي|غى)|"
    r"cancel|الغاء\s*الطلب|الغي\s*الطلب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COMPLETED_STATUSES = frozenset({
    "complete", "completed", "delivered", "fulfilled", "done",
    "تم التسليم", "مكتمل",
})

_CANCELLED_STATUSES = frozenset({
    "cancelled", "canceled", "ملغي", "ملغى",
})

_PAID_STATUSES = frozenset({
    "under_review", "pending_review", "payment_pending",
    "awaiting_receipt", "awaiting_payment_receipt", "awaiting_payment",
    "paid", "confirmed",
})


def _normalize_status(raw: str) -> str:
    return (raw or "").strip().lower()


def _parse_ts(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def infer_order_context_stage(state: Any) -> str:
    """Infer the active order-context stage from persisted brain state."""
    if state is None:
        return STAGE_NONE

    op = getattr(state, "order_prep", None)
    status = _normalize_status(str(getattr(op, "order_status", "") or ""))

    if status in _COMPLETED_STATUSES:
        return STAGE_CLOSED
    if status in _CANCELLED_STATUSES:
        return STAGE_CLOSED

    if getattr(op, "payment_receipt_received", False):
        return STAGE_PAID_ORDER
    if getattr(op, "awaiting_payment_receipt", False):
        return STAGE_AWAITING_FULFILLMENT
    if status in _PAID_STATUSES:
        return STAGE_PAID_ORDER

    missing = list(getattr(op, "missing_fields", None) or []) if op else []
    if missing:
        return STAGE_AWAITING_FULFILLMENT

    has_address = bool(
        op
        and (
            str(getattr(op, "city", "") or "").strip()
            or str(getattr(op, "short_address_code", "") or "").strip()
            or str(getattr(op, "google_maps_url", "") or "").strip()
        )
    )
    has_product = bool(
        str(getattr(op, "product_id", "") or "").strip()
        or getattr(state, "current_product_focus", None)
    )
    has_name = bool(str(getattr(op, "customer_first_name", "") or "").strip())
    has_draft = bool(str(getattr(state, "draft_order_id", "") or "").strip())

    if has_draft or (has_product and (has_name or has_address)):
        return STAGE_ORDER_DRAFT
    if has_product or getattr(op, "awaiting_variant_choice", False):
        return STAGE_PRODUCT_SELECTED

    if (
        list(getattr(state, "last_search_candidates", None) or [])
        or list(getattr(state, "catalog_browse_pool", None) or [])
        or str(getattr(state, "last_browse_query", "") or "").strip()
        or str(getattr(state, "stage", "") or "") in ("ordering", "deciding", "checkout")
    ):
        return STAGE_BROWSING

    return STAGE_NONE


def context_ttl_hours(stage: str) -> Optional[int]:
    return _TTL_HOURS.get(stage)


def is_active_order_context(state: Any, *, now: Optional[datetime] = None) -> bool:
    """True when order context is present and not expired."""
    stage = infer_order_context_stage(state)
    if stage in (STAGE_NONE, STAGE_CLOSED):
        return False
    if stage == STAGE_PAID_ORDER:
        return True
    return not is_context_expired(state, now=now)


def is_context_expired(state: Any, *, now: Optional[datetime] = None) -> bool:
    stage = infer_order_context_stage(state)
    ttl = context_ttl_hours(stage)
    if ttl is None:
        return False
    if ttl <= 0:
        return True
    anchor = _parse_ts(str(getattr(state, "updated_at", "") or ""))
    if anchor is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - anchor > timedelta(hours=ttl)


def detect_explicit_context_close(message: str, state: Any) -> Optional[str]:
    """Return close reason for cancel/complete signals."""
    msg = message or ""
    if _CANCEL_RE.search(msg):
        return "customer_cancelled"

    op = getattr(state, "order_prep", None)
    status = _normalize_status(str(getattr(op, "order_status", "") or ""))
    if status in _COMPLETED_STATUSES:
        return "order_delivered"
    if status in _CANCELLED_STATUSES:
        return "order_cancelled"
    return None


def clear_active_order_context(state: Any, *, reason: str) -> None:
    """Close active order objective; keep long-term customer memory fields."""
    if state is None:
        return

    logger.info(
        "[ORDER_CONTEXT_RESET] reason=%s had_focus=%s had_prep=%s stage=%s",
        reason,
        bool(getattr(state, "current_product_focus", None)),
        bool(getattr(state, "order_prep", None)),
        infer_order_context_stage(state),
    )

    from ..types import OrderPreparationState  # noqa: PLC0415

    state.current_product_focus = None
    state.draft_order_id = None
    state.checkout_url = None
    state.last_search_candidates = []
    state.catalog_browse_pool = []
    state.catalog_browse_offset = 0
    state.last_browse_query = ""
    state.last_recommended_products = []
    state.selected_variant = None
    state.cart_items = []
    state.pending_action = ""
    state.last_question_asked = ""
    state.order_prep = OrderPreparationState()
    if str(getattr(state, "stage", "") or "") in ("ordering", "deciding", "checkout"):
        state.stage = "discovery"


def maybe_reset_stale_order_context(
    state: Any,
    message: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    Apply reset rules before routing. Returns reset reason or ``None``.
    """
    if state is None:
        return None

    explicit = detect_explicit_context_close(message, state)
    if explicit:
        clear_active_order_context(state, reason=explicit)
        return explicit

    stage = infer_order_context_stage(state)
    if stage == STAGE_CLOSED:
        clear_active_order_context(state, reason="order_closed")
        return "order_closed"

    if stage == STAGE_PAID_ORDER:
        return None

    if str(getattr(state, "stage", "") or "").strip().lower() == "support":
        return None

    try:
        from .fresh_commerce_context import detect_explicit_order_resume  # noqa: PLC0415

        if detect_explicit_order_resume(message):
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[COMMERCE_CONTEXT_DECAY] resume probe skipped err=%s", exc)

    if is_context_expired(state, now=now):
        clear_active_order_context(state, reason=f"ttl_expired_{stage}")
        return f"ttl_expired_{stage}"

    try:
        from .fresh_commerce_context import (  # noqa: PLC0415
            maybe_reset_abandoned_commerce_on_fresh_exploration,
        )

        decay = maybe_reset_abandoned_commerce_on_fresh_exploration(state, message)
        if decay:
            return decay
    except Exception as exc:  # noqa: BLE001
        logger.debug("[COMMERCE_CONTEXT_DECAY] skipped err=%s", exc)

    return None


__all__ = [
    "STAGE_AWAITING_FULFILLMENT",
    "STAGE_BROWSING",
    "STAGE_CLOSED",
    "STAGE_NONE",
    "STAGE_ORDER_DRAFT",
    "STAGE_PAID_ORDER",
    "STAGE_PRODUCT_SELECTED",
    "clear_active_order_context",
    "context_ttl_hours",
    "detect_explicit_context_close",
    "infer_order_context_stage",
    "is_active_order_context",
    "is_context_expired",
    "maybe_reset_stale_order_context",
]
