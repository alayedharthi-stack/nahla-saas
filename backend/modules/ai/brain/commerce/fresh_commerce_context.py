"""
fresh_commerce_context.py
──────────────────────────
Abandoned draft / zombie order decay for fresh product exploration.

When a customer returns after a long gap with a catalog exploration question,
stale unconfirmed ``order_prep`` / ``current_product_focus`` must not hijack
routing into draft-order continuation. Platform-wide; no tenant hardcoding.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from .conversation_context_reset import (
    STAGE_PAID_ORDER,
    clear_active_order_context,
    infer_order_context_stage,
)

logger = logging.getLogger("nahla.brain.commerce.fresh_commerce")

COMMERCE_CONTEXT_GAP_DAYS = 7

_RESUME_ORDER_RE = re.compile(
    r"(?:"
    r"أ?(?:كمل|كمل|واصل)\s*(?:ال)?طلب|"
    r"الطلب\s*(?:ال)?(?:سابق|قديم|اللي\s+فات)|"
    r"أ?(?:بغى|بي|ابي|ابغى)\s+أ?(?:كمل|كمل)\s*(?:ال)?طلب|"
    r"ك(?:مل|مل)\s*(?:ال)?طلب\s*(?:ال)?(?:سابق|قديم)?"
    r")",
    re.I | re.UNICODE,
)

_FRESH_CATEGORY_EXPLORE_RE = re.compile(
    r"(?:^|\s)(?:وش|ايش|ايه|إيش|أيش|ما)\s+.+?(?:عندكم|عندك|لديكم|متوفر|"
    r"منتجات|انواع|أنواع|الانواع|الأنواع)",
    re.I | re.UNICODE,
)


def days_since_last_activity(state: Any) -> Optional[float]:
    raw = str(getattr(state, "updated_at", "") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return max(delta.total_seconds() / 86400.0, 0.0)
    except (TypeError, ValueError):
        return None


def has_confirmed_order(state: Any) -> bool:
    """True when checkout/payment evidence exists — not an abandoned draft."""
    stage = infer_order_context_stage(state)
    if stage == STAGE_PAID_ORDER:
        return True
    op = getattr(state, "order_prep", None)
    if op is None:
        return False
    if getattr(op, "payment_receipt_received", False):
        return True
    status = str(getattr(op, "order_status", "") or "").strip().lower()
    if status in {
        "pending_payment", "payment_pending", "awaiting_payment",
        "awaiting_receipt", "awaiting_payment_receipt",
    }:
        return True
    return False


def has_open_support_case(state: Any, *, human_priority: bool = False) -> bool:
    if human_priority:
        return True
    return str(getattr(state, "stage", "") or "").strip().lower() == "support"


def has_abandoned_unconfirmed_commerce(state: Any) -> bool:
    if has_confirmed_order(state):
        return False
    op = getattr(state, "order_prep", None)
    if op is not None:
        if str(getattr(op, "product_id", "") or "").strip():
            return True
        if list(getattr(op, "line_items", None) or getattr(op, "cart_items", None) or []):
            return True
        if list(getattr(op, "missing_fields", None) or []):
            return True
        if getattr(op, "awaiting_variant_choice", False):
            return True
    if getattr(state, "current_product_focus", None):
        return True
    if str(getattr(state, "draft_order_id", "") or "").strip():
        return True
    if str(getattr(state, "stage", "") or "") in ("ordering", "deciding", "checkout"):
        return True
    return False


def detect_explicit_order_resume(message: str) -> bool:
    return bool(_RESUME_ORDER_RE.search((message or "").strip()))


def is_fresh_exploratory_product_question(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    try:
        from .product_breadth_policy import (  # noqa: PLC0415
            explicit_soft_browse_requested,
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(text):
            return True
        if explicit_soft_browse_requested(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse policy import; regex fallback remains
        pass
    return bool(_FRESH_CATEGORY_EXPLORE_RE.search(text))


def should_reset_abandoned_commerce(
    *,
    message: str,
    state: Any,
    human_priority: bool = False,
) -> Tuple[bool, str]:
    if not has_abandoned_unconfirmed_commerce(state):
        return False, "no_abandoned_commerce"
    if has_confirmed_order(state):
        return False, "confirmed_order"
    if has_open_support_case(state, human_priority=human_priority):
        return False, "open_support_case"
    if detect_explicit_order_resume(message):
        return False, "explicit_order_resume"
    if not is_fresh_exploratory_product_question(message):
        return False, "not_fresh_exploration"
    gap = days_since_last_activity(state)
    if gap is not None and gap <= COMMERCE_CONTEXT_GAP_DAYS:
        return False, "within_gap_window"
    if gap is None:
        return True, "abandoned_draft_unknown_age_fresh_exploration"
    return True, "abandoned_draft_fresh_exploration"


def maybe_reset_abandoned_commerce_on_fresh_exploration(
    state: Any,
    message: str,
    *,
    human_priority: bool = False,
) -> Optional[str]:
    """Clear zombie draft context before routing; returns reset reason or None."""
    should, reason = should_reset_abandoned_commerce(
        message=message,
        state=state,
        human_priority=human_priority,
    )
    log_commerce_context_decay(
        state=state,
        message=message,
        expired=should,
        reason=reason,
        human_priority=human_priority,
    )
    if not should:
        return None
    clear_active_order_context(state, reason=reason)
    return reason


def log_commerce_context_decay(
    *,
    state: Any,
    message: str = "",
    expired: bool = False,
    reason: str = "",
    human_priority: bool = False,
    tenant_id: Optional[int] = None,
) -> None:
    try:
        op = getattr(state, "order_prep", None)
        focus = getattr(state, "current_product_focus", None) or {}
        focus_title = ""
        if isinstance(focus, dict):
            focus_title = str(focus.get("title") or focus.get("product_name") or "")[:80]
        gap = days_since_last_activity(state)
        logger.info(
            "[COMMERCE_CONTEXT_DECAY] tenant=%s order_age_days=%s "
            "last_customer_activity_days=%s stage=%s current_product_focus=%r "
            "order_prep_present=%s missing_fields=%s draft_order_id=%s "
            "expired=%s reason=%s fresh_explore=%s preview=%r",
            tenant_id if tenant_id is not None else "-",
            f"{gap:.1f}" if gap is not None else "-",
            f"{gap:.1f}" if gap is not None else "-",
            str(getattr(state, "stage", "") or "-"),
            focus_title or "-",
            bool(op and (
                str(getattr(op, "product_id", "") or "").strip()
                or list(getattr(op, "line_items", None) or [])
            )),
            ",".join(str(x) for x in (getattr(op, "missing_fields", None) or [])[:6]) or "-",
            str(getattr(state, "draft_order_id", "") or "-") or "-",
            expired,
            reason or "-",
            is_fresh_exploratory_product_question(message),
            (message or "")[:80],
        )
    except Exception:  # noqa: BLE001
        logger.exception("[COMMERCE_CONTEXT_DECAY] telemetry_emit_failed")


__all__ = [
    "COMMERCE_CONTEXT_GAP_DAYS",
    "days_since_last_activity",
    "detect_explicit_order_resume",
    "has_abandoned_unconfirmed_commerce",
    "has_confirmed_order",
    "has_open_support_case",
    "is_fresh_exploratory_product_question",
    "log_commerce_context_decay",
    "maybe_reset_abandoned_commerce_on_fresh_exploration",
    "should_reset_abandoned_commerce",
]
