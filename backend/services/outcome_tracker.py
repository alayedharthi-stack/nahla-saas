"""
services/outcome_tracker.py
────────────────────────────
Outcome Tracking — closes the analytics loop between the AI conversation
and confirmed real-world events (order confirmed, coupon redeemed).

Called from webhook_dispatcher.py when Salla fires:
  • order.updated  with status in CONFIRMED_STATUSES  → mark_order_confirmed()
  • order.created  with status in CONFIRMED_STATUSES  → mark_order_confirmed()

Design rules
────────────
  • Best-effort: every public function catches all exceptions and logs them.
    A failure here MUST NEVER block the webhook processing pipeline.
  • No PII in logs — phone numbers are masked before logging.
  • Idempotent: safe to call twice for the same order (second call is a no-op
    because order_confirmed is already True).
  • At most ONE ConversationTrace row is updated per call — we pick the most
    recent one where order_started=True and order_confirmed is False/NULL.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.outcome_tracker")

# Ensure database package is importable
_THIS    = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, ".."))
_DB_DIR  = os.path.abspath(os.path.join(_BACKEND, "../database"))
for _p in (_BACKEND, _DB_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from database.models import ConversationTrace as _ConversationTrace
    ConversationTrace = _ConversationTrace
except Exception:  # pragma: no cover — only fails in stripped test envs
    ConversationTrace = None  # type: ignore[assignment,misc]


def _normalize_phone(raw: str) -> str:
    """Module-level thin wrapper — allows tests to patch this symbol directly."""
    try:
        from services.store_sync import _normalize_phone as _sync_normalize  # noqa: PLC0415
        return _sync_normalize(raw)
    except Exception:
        return str(raw or "").strip()

# Salla order statuses that represent a confirmed/paid purchase
CONFIRMED_STATUSES = frozenset(
    {
        "confirmed",
        "paid",
        "in_progress",        # Salla: order being processed / packed
        "under_review",       # Salla: flagged for review but money received
        "completed",
        "delivered",
    }
)

# Coupon-applied statuses (Salla coupon.used / order discount applied)
COUPON_APPLIED_STATUSES = frozenset(
    {
        "coupon_applied",
        "discount_applied",
    }
)


def _mask_phone(phone: str) -> str:
    """Return last-4-digit masked version for logging."""
    s = str(phone or "").strip()
    if len(s) >= 4:
        return "*" * (len(s) - 4) + s[-4:]
    return "****"


def mark_order_confirmed(
    db: Any,
    tenant_id: int,
    order_data: Dict[str, Any],
) -> bool:
    """Update the most recent matching ConversationTrace with order_confirmed=True.

    Returns True if a row was updated, False otherwise (including on error).

    Matching logic:
      1. Extract the customer phone from order_data (supports Salla shape).
      2. Normalize the phone via the same helper used in store_sync.
      3. Find the newest ConversationTrace row where:
           tenant_id = tenant_id
           customer_phone LIKE the normalized phone   (prefix or exact)
           order_started = True
           order_confirmed IS NOT True
      4. Set order_confirmed = True and flush.
    """
    try:
        # ── Extract customer phone from Salla order payload ────────────────
        customer = order_data.get("customer") or order_data.get("customer_info") or {}
        if isinstance(customer, str):
            customer = {}
        raw_phone = (
            customer.get("mobile")
            or customer.get("phone")
            or order_data.get("customer_phone")
            or ""
        )
        normalized = _normalize_phone(raw_phone)
        if not normalized:
            logger.debug(
                "[OutcomeTracker] mark_order_confirmed: no phone in payload | "
                "tenant=%s order_id=%s",
                tenant_id,
                order_data.get("id", "?"),
            )
            return False

        # ── Find the trace to update ───────────────────────────────────────
        trace: Optional[Any] = (
            db.query(ConversationTrace)
            .filter(
                ConversationTrace.tenant_id      == tenant_id,
                ConversationTrace.customer_phone == normalized,
                ConversationTrace.order_started  == True,   # noqa: E712
                ConversationTrace.order_confirmed.isnot(True),
            )
            .order_by(ConversationTrace.created_at.desc())
            .first()
        )

        if trace is None:
            logger.debug(
                "[OutcomeTracker] no unconfirmed order_started trace found | "
                "tenant=%s phone=%s",
                tenant_id, _mask_phone(normalized),
            )
            return False

        trace.order_confirmed = True
        db.add(trace)
        db.commit()

        logger.info(
            "[OutcomeTracker] order_confirmed=True | "
            "tenant=%s phone=%s trace_id=%s order_id=%s",
            tenant_id,
            _mask_phone(normalized),
            trace.id,
            order_data.get("id", "?"),
        )
        return True

    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[OutcomeTracker] mark_order_confirmed failed (non-fatal) | "
            "tenant=%s error=%s",
            tenant_id, exc,
        )
        return False


def mark_coupon_redeemed(
    db: Any,
    tenant_id: int,
    order_data: Dict[str, Any],
) -> bool:
    """Mark the most recent matching ConversationTrace with coupon_redeemed=True.

    Called when the order payload contains a coupon/discount, indicating the
    AI-suggested coupon was actually used.  Mirrors mark_order_confirmed().
    """
    try:
        # Only flag if there's actually a coupon in the order
        has_coupon = bool(
            order_data.get("coupon")
            or order_data.get("discount_code")
            or order_data.get("coupon_code")
            or (order_data.get("amounts") or {}).get("discount")
        )
        if not has_coupon:
            return False

        customer = order_data.get("customer") or order_data.get("customer_info") or {}
        if isinstance(customer, str):
            customer = {}
        raw_phone = (
            customer.get("mobile")
            or customer.get("phone")
            or order_data.get("customer_phone")
            or ""
        )
        normalized = _normalize_phone(raw_phone)
        if not normalized:
            return False

        trace: Optional[Any] = (
            db.query(ConversationTrace)
            .filter(
                ConversationTrace.tenant_id      == tenant_id,
                ConversationTrace.customer_phone == normalized,
                ConversationTrace.order_started  == True,   # noqa: E712
                ConversationTrace.coupon_redeemed.isnot(True),
            )
            .order_by(ConversationTrace.created_at.desc())
            .first()
        )

        if trace is None:
            return False

        trace.coupon_redeemed = True
        db.add(trace)
        db.commit()

        logger.info(
            "[OutcomeTracker] coupon_redeemed=True | "
            "tenant=%s phone=%s trace_id=%s",
            tenant_id, _mask_phone(normalized), trace.id,
        )
        return True

    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(
            "[OutcomeTracker] mark_coupon_redeemed failed (non-fatal) | "
            "tenant=%s error=%s",
            tenant_id, exc,
        )
        return False


def record_order_outcome(
    db: Any,
    tenant_id: int,
    order_data: Dict[str, Any],
    event_type: str = "order.updated",
) -> None:
    """Convenience entry point — dispatches to the right trackers.

    Called from webhook_dispatcher._dispatch_salla() for confirmed orders.
    Never raises.
    """
    status = str(order_data.get("status") or "").strip().lower()

    if status in CONFIRMED_STATUSES:
        try:
            mark_order_confirmed(db, tenant_id, order_data)
        except Exception as exc:
            logger.debug("[OutcomeTracker] record_order_outcome mark_order_confirmed raised: %s", exc)
        try:
            mark_coupon_redeemed(db, tenant_id, order_data)
        except Exception as exc:
            logger.debug("[OutcomeTracker] record_order_outcome mark_coupon_redeemed raised: %s", exc)
