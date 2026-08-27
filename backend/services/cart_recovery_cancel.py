"""
services/cart_recovery_cancel.py
─────────────────────────────────
Hard-stop the abandoned-cart recovery flow the moment a customer actually
buys.

Why this exists
───────────────
The 4-stage cart-recovery workflow runs over ~24 hours. The customer can
convert at any time during that window — and they often do without ever
tapping a WhatsApp button (they go directly to the storefront). Without a
hard cancel hook, the next scheduled reminder fires after the purchase
and the customer reads "you forgot your cart" hours after they already
paid us. That single bad message destroys trust faster than any other
single failure mode in the product, so we treat it as a P0 invariant:

    "If a customer pays, we never send another abandoned-cart reminder."

The defence is layered in three places:

  1) ``conversion_layer.decide()`` — already short-circuits when
     ``ctx.order_completed`` is True (skip_reason="order_completed").
     This is the *pre-send guard* that runs at every step execution.

  2) ``automation_emitters.scan_abandoned_cart_followups`` — the
     sweeper that re-emits stages 2..N already calls
     ``_customer_has_completed_order_since`` and stops chasing converted
     customers. This is the *emission guard*.

  3) **This module** — *event-driven cancellation*. Triggered the moment
     ``handle_order_webhook`` lands a real order. We don't wait for the
     next sweeper tick or the next step execution; we walk every
     unprocessed recovery event for this customer right now and:

        • mark queued follow-up AutomationEvents as ``processed=True``
          so the engine never picks them up
        • flatten the parent event's ``recovery_followups`` so the
          sweeper treats every remaining stage as already-emitted
        • stamp ``converted=True``, ``remaining_steps_skipped=True`` and
          ``skip_reason="customer_purchased"`` on every parent
          AutomationExecution
        • flip the matching cart row(s) ``is_abandoned=False`` and stamp
          ``recovered_at`` so the dashboard "السلات المتروكة" stops
          showing them

The pre-send guard and the sweeper guard already protect us if this
event-driven path is somehow skipped (race conditions, dispatcher down,
etc.) — but the customer-facing latency is much better when we cancel
on the order webhook, because a postpone-rescheduled event with a
future ``created_at`` would otherwise sit in the queue and fire on time
even though the customer has already paid.

Idempotency
───────────
Safe to call multiple times. Already-processed events are no-ops; the
``recovery_converted_at`` payload key is the cheap idempotency token.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.cart_recovery_cancel")


# Number of recovery stages we ever schedule (stage 0 = template send,
# 1..3 = follow-ups). We pre-stamp anything in this range that hasn't
# fired yet. Bumping the workflow to more stages later is harmless —
# this just means we'd over-stamp by a few entries.
_MAX_RECOVERY_STAGES = 8

# Positive purchase evidence — unknown statuses are NOT purchases.
_PURCHASE_STATUSES_POSITIVE = frozenset({
    "completed", "delivered", "paid", "shipped", "out_for_delivery",
    "in_progress", "processing", "ready_for_pickup", "under_review",
})


def cancel_recovery_for_customer(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    reason: str = "customer_purchased",
    order_id: Optional[int] = None,
    order_external_id: Optional[str] = None,
    order_status: Optional[str] = None,
    matched_cart_external_id: Optional[str] = None,
    commit: bool = True,
) -> Dict[str, int]:
    """
    Cancel every still-pending abandoned-cart recovery step for the
    given customer.

    Returns a counter dict the caller can log / surface as metrics:

        {
          "events_cancelled":     <int>,  # queued follow-up events marked processed
          "parent_events_marked": <int>,  # stage-1 events with flattened followups
          "executions_stamped":   <int>,  # parent executions stamped with metrics
          "carts_recovered":      <int>,  # Order rows flipped is_abandoned=False
        }

    Safe to call repeatedly — already-processed work is detected via
    the ``recovery_converted_at`` payload key.
    """
    counters = {
        "events_cancelled":     0,
        "parent_events_marked": 0,
        "executions_stamped":   0,
        "carts_recovered":      0,
    }

    if not customer_id:
        return counters

    from models import (  # noqa: PLC0415
        AutomationEvent,
        AutomationExecution,
        Order,
    )

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now_naive.isoformat()

    # ── 1) Mark queued follow-up events as processed ─────────────────────
    #
    # These are the rescheduled / scheduled-future cart_abandoned events
    # that ``automation_emitters.scan_abandoned_cart_followups`` (or the
    # postpone reschedule) has queued. We must stop them BEFORE the
    # engine picks them up, because the engine's pre-send conversion-layer
    # check would also stop them — but only after burning a poll cycle
    # AND a "skipped" AutomationExecution row. Marking them processed
    # here keeps the audit trail clean.
    pending_events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.customer_id == customer_id,
            AutomationEvent.event_type == "cart_abandoned",
            AutomationEvent.processed == False,  # noqa: E712
        )
        .all()
    )
    for ev in pending_events:
        payload = dict(ev.payload or {})
        # Idempotency: if we've already cancelled this row, leave it alone.
        if payload.get("recovery_converted_at"):
            continue
        if matched_cart_external_id:
            ev_cart = str(payload.get("cart_external_id") or "").strip()
            ev_raw = str(payload.get("cart_id") or "").strip()
            wanted = str(matched_cart_external_id).strip()
            wanted_raw = wanted.replace("cart-", "", 1) if wanted.startswith("cart-") else wanted
            if ev_cart and ev_cart != wanted and ev_raw != wanted_raw:
                continue
        payload["recovery_converted_at"]   = now_iso
        payload["recovery_cancel_reason"]  = reason
        payload["recovery_cancel_order"]   = {
            "id":          order_id,
            "external_id": order_external_id,
            "status":      order_status,
        }
        ev.payload = payload
        try:
            flag_modified(ev, "payload")
        except Exception:
            pass
        ev.processed = True
        counters["events_cancelled"] += 1

    # ── 2) Flatten remaining stages on every parent stage-1 event ────────
    #
    # The sweeper looks at the original (stage_idx=0) event's
    # ``recovery_followups`` list to decide whether stages 2..N still
    # need to fire. Stamping every unfinished step as ``skipped`` here
    # guarantees the sweeper never re-emits them even if its internal
    # ``_customer_has_completed_order_since`` check becomes flaky.
    parent_events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.customer_id == customer_id,
            AutomationEvent.event_type == "cart_abandoned",
        )
        .all()
    )
    for parent in parent_events:
        payload = dict(parent.payload or {})
        # Stage-1 parents only — follow-up events have step_idx>0 and
        # we already handled those in the pending-events pass above.
        if int(payload.get("step_idx") or 0) > 0:
            continue
        if payload.get("recovery_converted_at"):
            continue

        progress = list(payload.get("recovery_followups") or [])
        seen = {int(p.get("step_idx", -1)) for p in progress}
        added = False
        for idx in range(1, _MAX_RECOVERY_STAGES):
            if idx in seen:
                continue
            progress.append({
                "step_idx":   idx,
                "skipped":    True,
                "reason":     reason,
                "emitted_at": now_iso,
            })
            added = True

        payload["recovery_followups"]      = progress
        payload["recovery_converted_at"]   = now_iso
        payload["recovery_cancel_reason"]  = reason
        payload["recovery_cancel_order"]   = {
            "id":          order_id,
            "external_id": order_external_id,
            "status":      order_status,
        }
        parent.payload = payload
        try:
            flag_modified(parent, "payload")
        except Exception:
            pass

        if added or "recovery_converted_at" not in (parent.payload or {}):
            counters["parent_events_marked"] += 1

    # ── 3) Stamp metrics on every parent AutomationExecution ─────────────
    #
    # The dashboard funnel report reads converted/skipped counters straight
    # from ``action_taken``. Without this stamp the recovery flow looks
    # like it gave up on its own rather than being short-circuited by a
    # real purchase, which makes the conversion-rate column lie.
    parent_event_ids = [p.id for p in parent_events]
    if parent_event_ids:
        executions = (
            db.query(AutomationExecution)
            .filter(
                AutomationExecution.tenant_id == tenant_id,
                AutomationExecution.event_id.in_(parent_event_ids),
            )
            .all()
        )
        for ex in executions:
            action = dict(ex.action_taken or {})
            metrics = dict(action.get("metrics") or {})
            if metrics.get("converted") and metrics.get("skip_reason") == reason:
                continue
            metrics["converted"]               = True
            metrics["remaining_steps_skipped"] = True
            metrics["skip_reason"]             = reason
            if order_id is not None:
                metrics["converted_order_id"] = order_id
            if order_external_id:
                metrics["converted_order_external_id"] = order_external_id
            metrics["converted_at"] = now_iso
            action["metrics"]      = metrics
            action["last_outcome"] = reason
            action["last_outcome_at"] = now_iso
            ex.action_taken = action
            try:
                flag_modified(ex, "action_taken")
            except Exception:
                pass
            counters["executions_stamped"] += 1

    # ── 4) Flip cart rows so the dashboard stops listing them ────────────
    #
    # The /autopilot/queues dashboard read is a flat
    # ``Order.is_abandoned == True`` filter. Until we flip the bit,
    # the merchant sees a cart in the abandoned queue even though the
    # customer has already paid for it. We flip every cart-namespaced
    # row for this customer that currently looks abandoned; the
    # ``cart-{id}`` prefix from sync_abandoned_carts keeps us from
    # ever touching a real Order row.
    abandoned_carts = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.is_abandoned == True,  # noqa: E712
        )
        .all()
    )
    for cart in abandoned_carts:
        info = cart.customer_info or {}
        from core.phone_coerce import coerce_customer_info_phone  # noqa: PLC0415

        cart_phone = coerce_customer_info_phone(info)
        if not cart_phone and not matched_cart_external_id:
            continue
        if matched_cart_external_id and cart.external_id != matched_cart_external_id:
            # When the caller knows exactly which cart converted (e.g. a
            # webhook with a cart_id), only flip that one. Otherwise
            # fall through to the phone-match path below.
            continue
        if not matched_cart_external_id:
            # Phone-based fallback — resolve the customer behind this cart
            # and only flip if it's the same person who just bought.
            from core.automation_emitters import _resolve_order_customer  # noqa: PLC0415
            cust = _resolve_order_customer(db, tenant_id, cart)
            if cust is None or cust.id != customer_id:
                continue

        cart.is_abandoned = False
        meta = dict(cart.extra_metadata or {})
        meta["recovered_at"]            = now_iso
        meta["recovered_via"]           = reason
        meta["recovered_order_id"]      = order_id
        meta["recovered_order_external"] = order_external_id
        cart.extra_metadata = meta
        try:
            flag_modified(cart, "extra_metadata")
        except Exception:
            pass
        counters["carts_recovered"] += 1

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

    if any(counters.values()):
        logger.info(
            "[CartRecoveryCancel] tenant=%s customer=%s reason=%s order_id=%s "
            "events_cancelled=%d parent_events_marked=%d executions_stamped=%d "
            "carts_recovered=%d",
            tenant_id, customer_id, reason, order_id,
            counters["events_cancelled"], counters["parent_events_marked"],
            counters["executions_stamped"], counters["carts_recovered"],
        )
    return counters


def order_is_a_purchase(status: Optional[str]) -> bool:
    """Return True only when the status is a documented positive purchase signal."""
    if not status:
        return False
    return str(status).strip().lower() in _PURCHASE_STATUSES_POSITIVE
