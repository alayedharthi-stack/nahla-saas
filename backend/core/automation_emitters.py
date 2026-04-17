"""
core/automation_emitters.py
────────────────────────────
Time-based event emitters that feed the automation engine.

Most automations are triggered reactively: the WhatsApp webhook, store_sync,
the storefront snippet, and the customer-intelligence pipeline write
`AutomationEvent` rows the moment something happens. But several of the
"smart autopilot" automations are *scheduled* — there is no real-time
signal that says "this cart has now been abandoned for 6 hours" or "this
COD order has not been confirmed yet". Those need a periodic scanner.

This module provides five such scanners:

  • `scan_unpaid_orders`              → recovery engine (online payment)
  • `scan_abandoned_cart_followups`   → recovery engine (cart stages 2 + 3)
  • `scan_cod_confirmations`          → recovery engine (COD reminder + cancel)
  • `scan_predictive_reorders`        → growth engine
  • `scan_calendar_events`            → growth engine (seasonal + salary)

All five are *event emitters only* (with one carved-out exception: the COD
sweeper performs the auto-cancel state mutation directly because that is
not a WhatsApp send — see its docstring). They write `AutomationEvent`
rows via `emit_automation_event` and let the existing automation engine
handle matching, conditions, idempotency, and sending. This keeps the
single automation engine guardrail intact: there is exactly one path to
`provider_send_message`.

Conflict prevention between sweepers
────────────────────────────────────
Each sweeper operates on a disjoint set of records:

  • `scan_unpaid_orders`             — Order.status in {pending,
    payment_pending, awaiting_payment, draft, new}. NEVER includes
    `pending_confirmation` (that is the COD half).
  • `scan_cod_confirmations`         — Order.status == "pending_confirmation"
    only. NEVER touches the unpaid bucket.
  • `scan_abandoned_cart_followups`  — re-emits AutomationEvents on the
    `cart_abandoned` event_type, never on Order rows. Stops as soon as
    the customer has placed a non-cancelled order with a creation time
    after the original abandon event.

So a single shopper who abandons a cart, comes back, and pays via online
payment will see at most one stage-1 cart reminder (from the snippet) and
zero unpaid-order or COD reminders, because the cart sweeper detects the
new completed order and the unpaid sweeper sees the order as paid. The
three flows are independently toggleable per tenant via SmartAutomation
rows; toggling one off does not affect the others.

Idempotency strategy
────────────────────
Each emitter uses the cheapest persistent marker for its domain:

  • Unpaid orders mark per-step progress in `Order.extra_metadata.unpaid_reminders`.
  • Predictive reorder uses the existing `PredictiveReorderEstimate.notified`
    boolean.
  • Calendar / salary use a per-tenant log inside
    `TenantSettings.extra_metadata.{calendar_emitter,salary_emitter}` keyed
    by `(slug, year)` and `YYYY-MM` respectively.

These markers are set on the same DB session as the event emit, so a crash
between the two cannot leave the system in a re-emit-forever state — both
roll back together.
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.automation_engine import emit_automation_event
from core.automation_triggers import AutomationTrigger
from core.calendar_events import events_for_date

logger = logging.getLogger("nahla.automation_emitters")

POLL_INTERVAL_SECONDS = 5 * 60  # 5 minutes — same cadence as webhook guardian

# Order statuses we treat as "still owes us money".
_PENDING_PAYMENT_STATUSES = frozenset({
    "pending",
    "pending_payment", "payment_pending", "awaiting_payment",
    "draft", "new",
})


# ── Unpaid order reminders ───────────────────────────────────────────────────

def scan_unpaid_orders(db: Session, tenant_id: int, *, now: Optional[datetime] = None) -> int:
    """
    Walk every order still in `pending`/`awaiting_payment` for this tenant
    and emit one `ORDER_PAYMENT_PENDING` event per step whose delay has
    elapsed but hasn't been emitted yet.

    Returns the number of events emitted.

    The step list comes from each enabled `unpaid_order_reminder`
    SmartAutomation's `config.steps` (default: 60m / 6h / 24h). Per-step
    progress is recorded in `Order.extra_metadata.unpaid_reminders`. An
    order that transitions out of the pending bucket is silently ignored
    on the next sweep — the engine's `stop_on_payment` semantics are
    enforced here at scan time rather than as a per-event condition.
    """
    from models import Customer, Order, SmartAutomation  # noqa: PLC0415

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    autos: List[Any] = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "unpaid_order_reminder",
            SmartAutomation.enabled.is_(True),
        )
        .all()
    )
    if not autos:
        return 0

    auto = autos[0]                       # one row per tenant by design
    config: Dict[str, Any] = auto.config or {}
    steps: List[Dict[str, Any]] = list(config.get("steps") or [])
    if not steps:
        # An enabled automation with no steps would emit forever — guard.
        logger.warning(
            "[Emitter] tenant=%s unpaid_order_reminder has no steps — skipping",
            tenant_id,
        )
        return 0

    # Pending orders for this tenant.
    orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.status.in_(_PENDING_PAYMENT_STATUSES),
        )
        .all()
    )
    if not orders:
        return 0

    emitted = 0
    for order in orders:
        created_at = _read_order_created_at(order)
        if created_at is None:
            continue
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

        meta: Dict[str, Any] = dict(order.extra_metadata or {})
        progress: List[Dict[str, Any]] = list(meta.get("unpaid_reminders") or [])
        already_sent_steps = {int(p.get("step_idx", -1)) for p in progress}

        # Resolve the customer (needed by the engine to send a message).
        customer = _resolve_order_customer(db, tenant_id, order)
        if customer is None:
            continue

        for step_idx, step in enumerate(steps):
            if step_idx in already_sent_steps:
                continue
            delay = int(step.get("delay_minutes") or 0)
            if (now - created_at) < timedelta(minutes=delay):
                # Steps are time-ordered — anything later is also too early.
                break
            payload: Dict[str, Any] = {
                "source":                "automation_emitters",
                "order_internal_id":     order.id,
                "order_id":              order.external_id,
                "external_order_number": order.external_order_number,
                "order_number":          order.external_order_number or order.external_id,
                "payment_url":           order.checkout_url,
                "checkout_url":          order.checkout_url,
                "step_idx":              step_idx,
                "message_type":          step.get("message_type") or "reminder",
            }
            emit_automation_event(
                db,
                tenant_id=tenant_id,
                event_type=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
                customer_id=customer.id,
                payload=payload,
                commit=False,
            )
            progress.append({
                "step_idx":   step_idx,
                "emitted_at": now.isoformat(),
            })
            emitted += 1

        if progress != list(meta.get("unpaid_reminders") or []):
            meta["unpaid_reminders"] = progress
            order.extra_metadata = meta

    if emitted:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s unpaid_orders emitted=%d", tenant_id, emitted,
        )
    return emitted


# ── Abandoned cart recovery (stages 2 and 3) ─────────────────────────────────

# Statuses that mean "the customer paid / completed the order in the
# meantime, so stop nudging them about the cart they abandoned earlier".
# Anything not in this set keeps the recovery flow running, so a refunded
# order or a cancelled order does NOT short-circuit the follow-ups.
_COMPLETED_ORDER_STATUSES = frozenset({
    "completed", "delivered", "paid", "shipped", "out_for_delivery",
    "in_progress", "processing", "ready_for_pickup",
    # COD orders that the customer has confirmed are also "no longer
    # abandoned" — they live in the COD funnel from this point on.
    "under_review",
})


def scan_abandoned_cart_followups(
    db: Session, tenant_id: int, *, now: Optional[datetime] = None,
) -> int:
    """
    Re-emit `cart_abandoned` events for stages 2 and 3 of the recovery
    workflow. Stage 1 is already emitted by the storefront snippet at
    abandonment time and processed by the engine after its 30-minute
    delay; this sweeper only owns the follow-up half.

    Returns the number of new follow-up events emitted.

    For each abandoned-cart event in the last 36 hours that has been
    processed (i.e. stage 1 was sent), we walk the automation's
    `config.steps` list and emit a *new* AutomationEvent of the same
    type for every stage whose delay has elapsed but hasn't been
    emitted yet. The new event carries `payload.step_idx = N` (1-based
    stage minus one) and `payload.parent_event_id` so we can audit the
    chain.

    Per-event progress is recorded on the *original* event's payload
    under `recovery_followups: [{step_idx, emitted_at}]`. This keeps
    the bookkeeping local to the row that owns the recovery thread —
    no new column needed, no second table to join.

    Stops as soon as the customer has placed any non-cancelled order
    after the abandonment timestamp. The check uses `_PENDING_PAYMENT_*`
    *exclusion* logic — anything that's not "still owes us money" and
    not "draft" is treated as a conversion. This is intentionally
    permissive: a refunded order still counts as a successful checkout
    for the purpose of "should we still chase this cart?".
    """
    from models import (  # noqa: PLC0415
        AutomationEvent, Customer, Order, SmartAutomation,
    )
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "abandoned_cart",
            SmartAutomation.enabled.is_(True),
        )
        .first()
    )
    if not auto:
        return 0

    config: Dict[str, Any] = auto.config or {}
    steps: List[Dict[str, Any]] = list(config.get("steps") or [])
    if len(steps) < 2:
        # Single-stage configs don't need the follow-up sweeper —
        # the original event already covers them.
        return 0

    horizon = now - timedelta(hours=36)
    candidates: List[Any] = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AutomationTrigger.CART_ABANDONED.value,
            AutomationEvent.processed.is_(True),
            AutomationEvent.created_at >= horizon,
        )
        .all()
    )
    if not candidates:
        return 0

    emitted = 0
    for original in candidates:
        # Only act on stage-1 events. A row that already carries
        # `step_idx` IS a follow-up emitted by an earlier sweep — never
        # use those as a parent (otherwise we'd cascade endlessly).
        original_payload: Dict[str, Any] = dict(original.payload or {})
        if int(original_payload.get("step_idx") or 0) > 0:
            continue

        progress: List[Dict[str, Any]] = list(
            original_payload.get("recovery_followups") or []
        )
        already_emitted = {int(p.get("step_idx", -1)) for p in progress}

        # Conversion guard — if the customer placed a real order after
        # the abandonment timestamp, stop chasing.
        if original.customer_id and _customer_has_completed_order_since(
            db, tenant_id=tenant_id, customer_id=original.customer_id,
            since=_naive(original.created_at),
        ):
            continue

        # Walk stages 2..N. We never re-emit stage 1 — it was the
        # original event itself.
        for step_idx, step in enumerate(steps):
            if step_idx == 0:
                continue
            if step_idx in already_emitted:
                continue
            delay = int(step.get("delay_minutes") or 0)
            if (now - _naive(original.created_at)) < timedelta(minutes=delay):
                # Steps are time-ordered — anything later is also too early.
                break

            new_payload: Dict[str, Any] = dict(original_payload)
            # Strip our own bookkeeping from the child payload — the
            # follow-up stands on its own and shouldn't carry the
            # parent's progress log.
            new_payload.pop("recovery_followups", None)
            new_payload.update({
                "source":           "automation_emitters.cart_followups",
                "step_idx":         step_idx,
                "parent_event_id":  original.id,
            })

            emit_automation_event(
                db,
                tenant_id=tenant_id,
                event_type=AutomationTrigger.CART_ABANDONED.value,
                customer_id=original.customer_id,
                payload=new_payload,
                commit=False,
            )
            progress.append({
                "step_idx":   step_idx,
                "emitted_at": now.isoformat(),
            })
            emitted += 1

        if progress != list(original_payload.get("recovery_followups") or []):
            original_payload["recovery_followups"] = progress
            original.payload = original_payload
            flag_modified(original, "payload")

    if emitted:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s cart_followups emitted=%d", tenant_id, emitted,
        )
    return emitted


# ── COD confirmation: reminder + auto-cancel ────────────────────────────────

def scan_cod_confirmations(
    db: Session, tenant_id: int, *, now: Optional[datetime] = None,
) -> int:
    """
    Drive the timed half of the COD confirmation flow.

    The synchronous half — sending the initial confirmation template
    and processing the customer's QUICK_REPLY tap — already lives in
    `routers/ai_sales.py::ai_sales_create_order` and
    `services/cod_confirmation.py::handle_cod_reply`. This sweeper
    owns what comes after that:

      • If the order has been in `pending_confirmation` for at least
        `config.reminder_after_minutes` (default 6 h) AND no reminder
        has been emitted yet, emit ORDER_COD_PENDING with `step_idx=0`
        so the engine renders `cod_confirmation_reminder_*` and ships
        it through the standard send path.

      • If the order has been in `pending_confirmation` for at least
        `config.cancel_after_minutes` (default 24 h), transition it
        directly to `cancelled`, stamp `extra_metadata.cod_auto_cancelled_at`,
        and log an `order.cod.auto_cancelled` event. Cancel is a state
        mutation, not a WhatsApp send, so it bypasses the engine — but
        the merchant still sees it in the event timeline.

    Returns the number of mutations performed (events emitted +
    orders cancelled).

    Per-order progress is stored on `Order.extra_metadata` under
    `cod_reminders: [{step_idx, emitted_at}]` and `cod_auto_cancelled_at`.
    """
    from models import Customer, Order, SmartAutomation  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "cod_confirmation",
            SmartAutomation.enabled.is_(True),
        )
        .first()
    )
    if not auto:
        return 0

    config: Dict[str, Any] = auto.config or {}
    reminder_after = int(config.get("reminder_after_minutes") or 360)  # 6 h
    cancel_after   = int(config.get("cancel_after_minutes") or 1440)   # 24 h
    if cancel_after <= reminder_after:
        # Misconfigured — bump cancel out by one reminder window so a
        # bad config can't cancel orders before any reminder ever fires.
        cancel_after = reminder_after + max(60, reminder_after)

    pending_orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
            Order.status == "pending_confirmation",
        )
        .all()
    )
    if not pending_orders:
        return 0

    mutations = 0
    for order in pending_orders:
        created_at = _read_order_created_at(order)
        if created_at is None:
            continue
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

        elapsed = now - created_at
        meta: Dict[str, Any] = dict(order.extra_metadata or {})
        progress: List[Dict[str, Any]] = list(meta.get("cod_reminders") or [])
        already_sent_steps = {int(p.get("step_idx", -1)) for p in progress}

        # ── Auto-cancel ──────────────────────────────────────────────
        if elapsed >= timedelta(minutes=cancel_after):
            from observability.event_logger import log_event  # noqa: PLC0415

            order.status = "cancelled"
            meta["cod_auto_cancelled_at"] = now.isoformat()
            meta["cod_auto_cancel_reason"] = "no_customer_response"
            order.extra_metadata = meta
            flag_modified(order, "extra_metadata")
            log_event(
                db, tenant_id, category="order",
                event_type="order.cod.auto_cancelled",
                summary=(
                    f"COD order #{order.id} auto-cancelled — no confirmation "
                    f"after {cancel_after} minutes"
                ),
                severity="warning",
                payload={
                    "order_id":             order.id,
                    "elapsed_minutes":      int(elapsed.total_seconds() // 60),
                    "cancel_after_minutes": cancel_after,
                },
                reference_id=str(order.id),
            )
            mutations += 1
            continue

        # ── Reminder ─────────────────────────────────────────────────
        if elapsed < timedelta(minutes=reminder_after):
            continue
        # We use step_idx=0 so the engine resolves `steps[0]` of the
        # cod_confirmation seed — the reminder template. Idempotency is
        # tracked here, not on AutomationExecution, because there is
        # only ever one reminder per order in this flow and the engine's
        # standard (event_id, automation_id) idempotency already covers
        # the engine-side dedup of the emitted event.
        if 0 in already_sent_steps:
            continue

        customer = _resolve_order_customer(db, tenant_id, order)
        if customer is None:
            continue

        payload: Dict[str, Any] = {
            "source":                "automation_emitters.cod_confirmation",
            "order_internal_id":     order.id,
            "order_id":              order.external_id,
            "external_order_number": order.external_order_number,
            "order_number":          order.external_order_number or order.external_id,
            "step_idx":              0,
            "message_type":          "reminder",
        }
        emit_automation_event(
            db,
            tenant_id=tenant_id,
            event_type=AutomationTrigger.ORDER_COD_PENDING.value,
            customer_id=customer.id,
            payload=payload,
            commit=False,
        )
        progress.append({"step_idx": 0, "emitted_at": now.isoformat()})
        meta["cod_reminders"] = progress
        order.extra_metadata = meta
        flag_modified(order, "extra_metadata")
        mutations += 1

    if mutations:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s cod_confirmation mutations=%d", tenant_id, mutations,
        )
    return mutations


def _customer_has_completed_order_since(
    db: Session, *, tenant_id: int, customer_id: int, since: datetime,
) -> bool:
    """Return True iff the customer has at least one non-pending,
    non-cancelled order created after ``since`` for this tenant.

    Used by the abandoned-cart sweeper to stop chasing customers who
    have already converted. Errs on the side of "still abandoned" if
    we can't resolve a customer order timestamp — better to send one
    extra reminder than to silently drop the funnel.
    """
    from models import Order  # noqa: PLC0415

    if not customer_id:
        return False

    orders = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id,
        )
        .all()
    )
    for o in orders:
        if o.status in _PENDING_PAYMENT_STATUSES:
            continue
        if o.status in {"cancelled", "refunded", "pending_confirmation"}:
            continue
        # The customer link on Order is only via customer_info.phone, so
        # we resolve through the customer row to avoid false negatives.
        candidate = _resolve_order_customer(db, tenant_id, o)
        if candidate is None or candidate.id != customer_id:
            continue
        created_at = _read_order_created_at(o)
        if created_at is None:
            continue
        created_naive = _naive(created_at)
        if created_naive >= since:
            return True
    return False


def _naive(dt: Optional[datetime]) -> datetime:
    """Best-effort UTC-naive coercion for cross-row datetime math."""
    if dt is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# ── Predictive reorder reminders ─────────────────────────────────────────────

def scan_predictive_reorders(db: Session, tenant_id: int, *, now: Optional[datetime] = None) -> int:
    """
    For each `PredictiveReorderEstimate` whose `predicted_reorder_date` is
    inside the configured `days_before` window and that hasn't been notified
    yet, emit one `PREDICTIVE_REORDER_DUE` event and flip the row's
    `notified` flag so it doesn't fire again.
    """
    from models import PredictiveReorderEstimate, Product, SmartAutomation  # noqa: PLC0415

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "predictive_reorder",
            SmartAutomation.enabled.is_(True),
        )
        .first()
    )
    if not auto:
        return 0

    config: Dict[str, Any] = auto.config or {}
    days_before = int(config.get("days_before") or 3)
    horizon = now + timedelta(days=days_before)

    estimates: List[Any] = (
        db.query(PredictiveReorderEstimate)
        .filter(
            PredictiveReorderEstimate.tenant_id == tenant_id,
            PredictiveReorderEstimate.notified.is_(False),
            PredictiveReorderEstimate.predicted_reorder_date.isnot(None),
            PredictiveReorderEstimate.predicted_reorder_date <= horizon,
        )
        .all()
    )
    if not estimates:
        return 0

    products_by_id: Dict[int, Any] = {
        p.id: p for p in db.query(Product).filter(
            Product.tenant_id == tenant_id,
            Product.id.in_({e.product_id for e in estimates}),
        ).all()
    }

    emitted = 0
    for est in estimates:
        product = products_by_id.get(est.product_id)
        payload: Dict[str, Any] = {
            "source":                "automation_emitters",
            "estimate_id":           est.id,
            "product_name":          getattr(product, "title", None) or "",
            "product_external_id":   getattr(product, "external_id", None),
            "predicted_reorder_at":  est.predicted_reorder_date.isoformat() if est.predicted_reorder_date else None,
        }
        emit_automation_event(
            db,
            tenant_id=tenant_id,
            event_type=AutomationTrigger.PREDICTIVE_REORDER_DUE.value,
            customer_id=est.customer_id,
            payload=payload,
            commit=False,
        )
        est.notified = True
        emitted += 1

    if emitted:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s predictive_reorders emitted=%d", tenant_id, emitted,
        )
    return emitted


# ── Calendar (seasonal + salary payday) ──────────────────────────────────────

def scan_calendar_events(db: Session, tenant_id: int, *, today: Optional[_date] = None) -> int:
    """
    Fire `SEASONAL_EVENT_DUE` for each entry in the built-in Saudi calendar
    that lands tomorrow (so the offer hits inboxes the day before, not on
    the holiday itself), and `SALARY_PAYDAY_DUE` one day before each
    tenant's configured payday.

    Per-tenant dedup is stored on `TenantSettings.extra_metadata`:
      • `calendar_emitter[slug] = "YYYY"`     — one fan-out per slug per year
      • `salary_emitter         = "YYYY-MM"`  — one fan-out per month

    Returns the total number of events emitted across both flows.
    """
    today = today or _date.today()
    target_day = today + timedelta(days=1)

    seasonal = _scan_seasonal(db, tenant_id, target_day=target_day)
    salary = _scan_salary(db, tenant_id, today=today, target_day=target_day)
    return seasonal + salary


def _scan_seasonal(db: Session, tenant_id: int, *, target_day: _date) -> int:
    from models import SmartAutomation, TenantSettings  # noqa: PLC0415

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "seasonal_offer",
            SmartAutomation.enabled.is_(True),
        )
        .first()
    )
    if not auto:
        return 0

    upcoming = events_for_date(target_day)
    if not upcoming:
        return 0

    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    extra: Dict[str, Any] = dict(getattr(settings, "extra_metadata", None) or {})
    log: Dict[str, str] = dict(extra.get("calendar_emitter") or {})

    emitted = 0
    config: Dict[str, Any] = auto.config or {}
    audience: Dict[str, Any] = dict(config.get("audience") or {})

    customers = _select_audience(db, tenant_id, audience)
    if not customers:
        return 0

    for ev in upcoming:
        already = log.get(ev.slug)
        if already == str(target_day.year):
            continue
        for cust in customers:
            payload = {
                "source":         "automation_emitters",
                "event_slug":     ev.slug,
                "event_category": ev.category,
                "occasion_name":  ev.name_ar,
            }
            emit_automation_event(
                db,
                tenant_id=tenant_id,
                event_type=AutomationTrigger.SEASONAL_EVENT_DUE.value,
                customer_id=cust.id,
                payload=payload,
                commit=False,
            )
            emitted += 1
        log[ev.slug] = str(target_day.year)

    if emitted and settings is not None:
        extra["calendar_emitter"] = log
        settings.extra_metadata = extra

    if emitted:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s seasonal_offer emitted=%d (events=%s)",
            tenant_id, emitted, [ev.slug for ev in upcoming],
        )
    return emitted


def _scan_salary(db: Session, tenant_id: int, *, today: _date, target_day: _date) -> int:
    from models import SmartAutomation, TenantSettings  # noqa: PLC0415

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "salary_payday_offer",
            SmartAutomation.enabled.is_(True),
        )
        .first()
    )
    if not auto:
        return 0

    config: Dict[str, Any] = auto.config or {}
    payday_day = int(config.get("payday_day") or 27)
    payday_day = max(1, min(payday_day, 28))   # cap at 28 so Feb is always covered
    if target_day.day != payday_day:
        return 0

    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    extra: Dict[str, Any] = dict(getattr(settings, "extra_metadata", None) or {})
    last_run = (extra.get("salary_emitter") or {}).get("last_month_emitted")
    month_key = f"{today.year:04d}-{today.month:02d}"
    if last_run == month_key:
        return 0

    audience: Dict[str, Any] = dict(config.get("audience") or {})
    customers = _select_audience(db, tenant_id, audience)
    if not customers:
        return 0

    emitted = 0
    for cust in customers:
        payload = {
            "source":         "automation_emitters",
            "payday_day":     payday_day,
            "occasion_name":  "اقتراب الراتب",
        }
        emit_automation_event(
            db,
            tenant_id=tenant_id,
            event_type=AutomationTrigger.SALARY_PAYDAY_DUE.value,
            customer_id=cust.id,
            payload=payload,
            commit=False,
        )
        emitted += 1

    if emitted and settings is not None:
        extra["salary_emitter"] = {"last_month_emitted": month_key}
        settings.extra_metadata = extra

    if emitted:
        db.commit()
        logger.info(
            "[Emitter] tenant=%s salary_payday_offer emitted=%d (month=%s)",
            tenant_id, emitted, month_key,
        )
    return emitted


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_order_created_at(order: Any) -> Optional[datetime]:
    """
    `Order` has no first-class `created_at`. Tools that need it look in
    `extra_metadata.created_at` first, then fall back to a couple of
    plausible alternatives. Mirrors `routers.orders._read_created_at` so
    the emitter and the dashboard agree on the same timestamp.
    """
    meta = getattr(order, "extra_metadata", None) or {}
    candidates: List[Any] = [
        meta.get("created_at"),
        getattr(order, "created_at", None),
        meta.get("updated_at"),
    ]
    for cand in candidates:
        if isinstance(cand, datetime):
            return cand
        if not cand:
            continue
        text = str(cand).strip()
        for variant in (
            text.replace("Z", "+00:00"),
            text.replace(" ", "T", 1),
            text.split(".", 1)[0].replace(" ", "T", 1),
        ):
            try:
                return datetime.fromisoformat(variant)
            except Exception:
                continue
    return None


def _resolve_order_customer(db: Session, tenant_id: int, order: Any) -> Optional[Any]:
    from models import Customer  # noqa: PLC0415

    info = getattr(order, "customer_info", None) or {}
    phone = (info.get("phone") or info.get("mobile") or "").strip()
    if not phone:
        return None
    cust = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.phone == phone)
        .first()
    )
    if cust:
        return cust
    digits = phone.lstrip("+").replace(" ", "").replace("-", "")
    if not digits:
        return None
    return (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.phone.endswith(digits))
        .first()
    )


def _select_audience(db: Session, tenant_id: int, audience: Dict[str, Any]) -> List[Any]:
    """
    Build the broadcast audience for seasonal/salary fan-out.

    Default is conservative: customers that have at least one historical
    order. The merchant can tune via `audience.min_orders` and
    `audience.max_inactive_days`. We deliberately do NOT broadcast to leads
    or anonymous storefront visitors — that would burn template quota.
    """
    from models import Customer, CustomerProfile  # noqa: PLC0415

    min_orders = int(audience.get("min_orders") or 1)
    max_inactive_days = audience.get("max_inactive_days")

    rows = (
        db.query(CustomerProfile, Customer)
        .join(Customer, Customer.id == CustomerProfile.customer_id)
        .filter(
            CustomerProfile.tenant_id == tenant_id,
            Customer.phone.isnot(None),
        )
        .all()
    )
    out: List[Any] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (
        now - timedelta(days=int(max_inactive_days))
        if max_inactive_days is not None
        else None
    )
    for prof, cust in rows:
        total_orders = int(getattr(prof, "total_orders", 0) or 0)
        if total_orders < min_orders:
            continue
        if cutoff is not None:
            last = getattr(prof, "last_order_at", None)
            if last is None:
                continue
            last_naive = last.replace(tzinfo=None) if last.tzinfo else last
            if last_naive < cutoff:
                continue
        out.append(cust)
    return out


# ── Scheduler entry point ────────────────────────────────────────────────────

import asyncio  # noqa: E402  (kept at bottom — only used by the scheduler)


async def run_automation_emitters_scheduler() -> None:
    """
    Background loop — sweeps the three emitters for every active tenant
    every `POLL_INTERVAL_SECONDS`. Started from `core/scheduler.py` and
    wired into the FastAPI startup event in `backend/main.py`.
    """
    from core.database import SessionLocal  # noqa: PLC0415
    from models import Tenant  # noqa: PLC0415

    await asyncio.sleep(60)   # give the app time to come up
    logger.info(
        "[EmittersScheduler] started — polling every %ds", POLL_INTERVAL_SECONDS
    )

    while True:
        try:
            db: Session = SessionLocal()
            try:
                tenants: List[Any] = (
                    db.query(Tenant)
                    .filter(Tenant.is_active.is_(True))
                    .all()
                )
                for tenant in tenants:
                    try:
                        scan_unpaid_orders(db, tenant.id)
                        scan_abandoned_cart_followups(db, tenant.id)
                        scan_cod_confirmations(db, tenant.id)
                        scan_predictive_reorders(db, tenant.id)
                        scan_calendar_events(db, tenant.id)
                    except Exception as exc:
                        logger.error(
                            "[EmittersScheduler] tenant=%s failed: %s",
                            tenant.id, exc, exc_info=True,
                        )
                        try:
                            db.rollback()
                        except Exception:
                            pass
            finally:
                db.close()
        except Exception as exc:
            logger.error("[EmittersScheduler] cycle error: %s", exc, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
