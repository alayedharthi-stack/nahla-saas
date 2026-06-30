"""
core/automation_engine.py
──────────────────────────
Event-Driven Automation Engine.

Processing flow
───────────────
  1. AutomationEvent rows with processed=False are written by event emitters
     (whatsapp_webhook, store_sync, webhooks, customer_intelligence, tracking).
  2. Every 60 s the scheduler calls process_pending_events(db, tenant_id).
  3. For each unprocessed event the engine:
       a. Finds all enabled SmartAutomations whose trigger_event matches.
       b. Checks idempotency via AutomationExecution (one row per event+automation).
       c. Checks delay: event.created_at + delay_minutes <= NOW.
       d. Evaluates conditions from automation.config (customer_status, min_spent, …).
       e. Executes the action (sends WhatsApp template).
       f. Writes an AutomationExecution row (sent | skipped | failed).
       g. Updates automation stats_triggered / stats_sent.
       h. Marks AutomationEvent.processed = True once all matched automations
          have a final execution record.

Public API
──────────
  emit_automation_event(db, tenant_id, event_type, customer_id, payload, commit=False)
      → call from event emitters; inserts an AutomationEvent row.

  process_pending_events(db, tenant_id) → int
      → call from scheduler; returns number of actions taken.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.customer_display import display_name_passthrough_or_fallback

logger = logging.getLogger("nahla.automation_engine")

# Automations older than this are not retried (event was too stale to be relevant)
_MAX_EVENT_AGE_HOURS = 72
# Polling interval used by the scheduler (seconds)
POLL_INTERVAL_SECONDS = 60
# Max events processed per tenant per cycle
_BATCH_SIZE = 100


# ── Helpers ───────────────────────────────────────────────────────────────────

def _naive_utc(dt: Optional[datetime]) -> datetime:
    """Return dt as a naive UTC datetime (strip tz if present)."""
    if dt is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Public: event emitter helper ─────────────────────────────────────────────

def emit_automation_event(
    db: Session,
    tenant_id: int,
    event_type: str,
    customer_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    *,
    commit: bool = False,
) -> Any:
    """
    Insert an AutomationEvent row with processed=False.

    Called by event sources (whatsapp_webhook, store_sync, webhooks,
    customer_intelligence).  Does NOT commit by default — the caller controls
    the transaction boundary.

    Legacy event names (e.g. "abandoned_cart") are aliased to their canonical
    AutomationTrigger value at write time so the engine's exact-match lookup
    resolves correctly even if a caller hasn't been migrated yet.
    """
    from core.automation_triggers import LEGACY_EVENT_ALIASES  # noqa: PLC0415
    from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415
    from models import AutomationEvent  # noqa: PLC0415

    original_type = event_type
    aliased = LEGACY_EVENT_ALIASES.get(event_type)
    if aliased is not None:
        event_type = aliased.value
        _log_event(
            _EVENTS.AUTOMATION_EVENT_ALIASED,
            tenant_id=tenant_id,
            customer_id=customer_id,
            from_event_type=original_type,
            to_event_type=event_type,
        )

    ev = AutomationEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        customer_id=customer_id,
        payload=payload or {},
        processed=False,
        created_at=_utcnow_naive(),
    )
    db.add(ev)
    if commit:
        db.commit()
    else:
        db.flush()

    _log_event(
        _EVENTS.AUTOMATION_EVENT_EMITTED,
        tenant_id=tenant_id,
        customer_id=customer_id,
        event_type=event_type,
    )
    return ev


# ── Public: main processing entry point ──────────────────────────────────────

def _is_autopilot_enabled(db: Session, tenant_id: int) -> bool:
    """
    Read the master autopilot switch out of `TenantSettings.extra_metadata`.

    Default is False so a tenant that has never touched the toggle does NOT
    have automated WhatsApp messages going out — the merchant must opt in
    explicitly. Mirrors the contract `routers.automations._get_autopilot_settings`
    enforces, but kept import-light here so the engine doesn't pull the
    whole router on every cycle.
    """
    from models import TenantSettings  # noqa: PLC0415

    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    if settings is None:
        return False
    extra = getattr(settings, "extra_metadata", None) or {}
    autopilot = extra.get("autopilot") or {}
    if "enabled" in autopilot:
        return bool(autopilot.get("enabled"))
    # Backward compat: older tenants stored the flag inside ai_settings.
    ai = getattr(settings, "ai_settings", None) or {}
    return bool(ai.get("autopilot_enabled"))


async def process_pending_events(
    db: Session,
    tenant_id: int,
    *,
    skip_autopilot_check: bool = False,
    event_ids: Optional[List[int]] = None,
) -> int:
    """
    Scan and process unprocessed AutomationEvent rows for one tenant.
    Returns the total number of WhatsApp messages sent in this cycle.

    Args:
        skip_autopilot_check: bypass only the autopilot-enabled toggle
            (for manual retries). Billing/trial guard is ALWAYS enforced.
        event_ids: if provided, only process these specific events
    """
    from core.automations_seed import (  # noqa: PLC0415
        ensure_default_promotions_for_tenant,
        ensure_engine_for_tenant,
        ensure_trigger_event_for_tenant,
    )
    from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415
    from models import AutomationEvent, AutomationExecution  # noqa: PLC0415

    # Defensive repair: if any SmartAutomation row has a stale/NULL
    # trigger_event (e.g. a tenant was seeded before migration 0024 ran),
    # normalise it now so this cycle can actually match. Cheap no-op on
    # already-healthy tenants.
    try:
        repaired = ensure_trigger_event_for_tenant(db, tenant_id)
        if repaired:
            _log_event(
                _EVENTS.AUTOMATION_SEED_REPAIRED,
                tenant_id=tenant_id,
                rows_repaired=repaired,
            )
            db.flush()
        # Same defensive repair for the `engine` column added in 0027 so
        # tenants seeded before the migration land in the correct dashboard
        # bucket on first cycle.
        ensure_engine_for_tenant(db, tenant_id)
        # Auto-seed the default Promotions referenced by promotion-backed
        # automations (seasonal_offer, salary_payday_offer) and link each
        # automation's `config.promotion_id` if missing. Cheap no-op once
        # the rows exist.
        ensure_default_promotions_for_tenant(db, tenant_id)
    except Exception as exc:
        logger.error(
            "[AutoEngine] trigger_event repair failed tenant=%s: %s",
            tenant_id, exc, exc_info=True,
        )

    # NOTE: Billing/trial guard is NOT applied here.
    # Inbound event scanning, matching, and recording run for ALL tenants.
    # The outbound guard (has_billing_access) is enforced inside _execute_action
    # immediately before any WhatsApp message is sent — see that function below.

    # ── Master autopilot switch (skipped for manual retries) ──────────
    if not skip_autopilot_check and not _is_autopilot_enabled(db, tenant_id):
        skipped = _drain_pending_for_disabled_autopilot(db, tenant_id)
        if skipped:
            _log_event(
                _EVENTS.AUTOMATION_AUTOPILOT_DISABLED,
                level=logging.INFO,
                tenant_id=tenant_id,
                events_drained=skipped,
            )
        return 0

    now = _utcnow_naive()
    cutoff = now - timedelta(hours=_MAX_EVENT_AGE_HOURS)

    if event_ids:
        events = list(
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.id.in_(event_ids),
                AutomationEvent.processed.is_(False),
            )
            .all()
        )
    else:
        events = list(
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.processed.is_(False),
                AutomationEvent.created_at >= cutoff,
            )
            .order_by(AutomationEvent.created_at.asc())
            .limit(_BATCH_SIZE)
            .all()
        )

    if not events:
        return 0

    total_sent = 0
    for event in events:
        sent = await _process_event(db, tenant_id, event, now)
        total_sent += sent

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("[AutoEngine] Commit failed tenant=%s: %s", tenant_id, exc)

    if total_sent > 0:
        logger.info(
            "[AutoEngine] tenant=%s cycle complete — sent=%d",
            tenant_id, total_sent,
        )
    return total_sent


def _drain_pending_for_disabled_autopilot(db: Session, tenant_id: int) -> int:
    """
    Mark every still-unprocessed AutomationEvent for this tenant as resolved
    with one `skipped(autopilot_disabled)` AutomationExecution per matched
    automation. Called when the master switch is OFF so the queue does not
    grow unbounded while autopilot is paused.

    Idempotent: events already marked processed are left alone, and matched
    automations that already have an execution record (sent/skipped/failed)
    are not re-recorded.
    """
    from models import AutomationEvent, AutomationExecution, SmartAutomation  # noqa: PLC0415

    cutoff = _utcnow_naive() - timedelta(hours=_MAX_EVENT_AGE_HOURS)

    events: List[Any] = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.processed.is_(False),
            AutomationEvent.created_at >= cutoff,
        )
        .order_by(AutomationEvent.created_at.asc())
        .limit(_BATCH_SIZE)
        .all()
    )
    if not events:
        return 0

    drained = 0
    for event in events:
        matches: List[Any] = (
            db.query(SmartAutomation)
            .filter(
                SmartAutomation.tenant_id == tenant_id,
                SmartAutomation.trigger_event == event.event_type,
            )
            .all()
        )
        for auto in matches:
            existing = (
                db.query(AutomationExecution)
                .filter(
                    AutomationExecution.event_id == event.id,
                    AutomationExecution.automation_id == auto.id,
                )
                .first()
            )
            if existing:
                continue
            _write_execution(
                db,
                event.id,
                auto.id,
                event.customer_id,
                tenant_id,
                status="skipped",
                skip_reason="autopilot_disabled",
            )
        event.processed = True
        drained += 1

    if drained:
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("[AutoEngine] drain commit failed tenant=%s: %s", tenant_id, exc)
            return 0
    return drained


def _drain_pending_for_reason(db: Session, tenant_id: int, reason: str) -> int:
    """Drain pending events with a custom skip reason (e.g. trial_expired)."""
    from models import AutomationEvent, AutomationExecution, SmartAutomation  # noqa: PLC0415

    cutoff = _utcnow_naive() - timedelta(hours=_MAX_EVENT_AGE_HOURS)
    events: List[Any] = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.processed.is_(False),
            AutomationEvent.created_at >= cutoff,
        )
        .order_by(AutomationEvent.created_at.asc())
        .limit(_BATCH_SIZE)
        .all()
    )
    if not events:
        return 0

    drained = 0
    for event in events:
        matches: List[Any] = (
            db.query(SmartAutomation)
            .filter(
                SmartAutomation.tenant_id == tenant_id,
                SmartAutomation.trigger_event == event.event_type,
            )
            .all()
        )
        for auto in matches:
            existing = (
                db.query(AutomationExecution)
                .filter(
                    AutomationExecution.event_id == event.id,
                    AutomationExecution.automation_id == auto.id,
                )
                .first()
            )
            if existing:
                continue
            _write_execution(
                db, event.id, auto.id, event.customer_id, tenant_id,
                status="skipped", skip_reason=reason,
            )
        event.processed = True
        drained += 1

    if drained:
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("[AutoEngine] drain(%s) commit failed tenant=%s: %s", reason, tenant_id, exc)
            return 0
    return drained


# ── Internal: event processing ────────────────────────────────────────────────

async def _process_event(
    db: Session, tenant_id: int, event: Any, now: datetime
) -> int:
    """
    Find matching automations for one event and try to execute each.
    Returns the number of messages actually sent.
    """
    from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415
    from models import SmartAutomation  # noqa: PLC0415

    # Any row whose trigger_event matches, regardless of enabled — used to
    # distinguish "unmatched trigger" (no row with this trigger_event at all)
    # from "no enabled automation" (rows exist but all disabled).
    all_matches: List[Any] = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.trigger_event == event.event_type,
        )
        .all()
    )
    automations: List[Any] = [a for a in all_matches if a.enabled]

    if not automations:
        # Previously this branch called `logger.debug(...)` and silently set
        # processed=True. That hid every trigger-name mismatch in production.
        # Now we emit a structured WARNING log so the drift is searchable in
        # Railway logs, but we still set processed=True because reprocessing
        # on every cycle would only flood the logs with the same failure.
        if not all_matches:
            # Demoted from WARNING to INFO: this is the *expected* state when
            # a tenant simply hasn't configured a SmartAutomation for the
            # given trigger event. It does NOT affect the AI / order flow,
            # but the WARNING level was confusing operators into thinking
            # the brain pipeline failed when in fact it never even ran for
            # this row. Includes a `note` field to make that clear.
            _log_event(
                _EVENTS.AUTOMATION_UNMATCHED_TRIGGER,
                level=logging.INFO,
                tenant_id=tenant_id,
                event_id=event.id,
                event_type=event.event_type,
                customer_id=event.customer_id,
                reason="no_smart_automation_row_has_this_trigger_event",
                note="benign — no automation configured for this trigger; AI / order flow unaffected",
            )
        else:
            _log_event(
                _EVENTS.AUTOMATION_NO_AUTOMATION_FOUND,
                level=logging.WARNING,
                tenant_id=tenant_id,
                event_id=event.id,
                event_type=event.event_type,
                customer_id=event.customer_id,
                reason="all_matching_automations_disabled",
                matching_automation_ids=[a.id for a in all_matches],
            )
        event.processed = True
        return 0

    sent = 0
    all_resolved = True  # True when every automation has a final execution record

    for automation in automations:
        result = await _try_execute(db, tenant_id, event, automation, now)
        if result == "sent":
            sent += 1
        elif result == "delay":
            # Delay not yet elapsed — revisit next cycle
            all_resolved = False

    if all_resolved:
        event.processed = True
        if automations:
            event.automation_id = automations[-1].id

    return sent


async def _try_execute(
    db: Session, tenant_id: int, event: Any, automation: Any, now: datetime
) -> str:
    """
    Attempt to execute one automation against one event.

    Returns one of: 'sent' | 'skipped' | 'failed' | 'delay' | 'duplicate'
    """
    from models import AutomationExecution  # noqa: PLC0415

    # ── Idempotency ───────────────────────────────────────────────────────────
    existing: Optional[Any] = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.event_id == event.id,
            AutomationExecution.automation_id == automation.id,
        )
        .first()
    )
    if existing:
        logger.debug(
            "[AutoEngine] Already executed event=%s automation=%s status=%s — skip",
            event.id, automation.id, existing.status,
        )
        return existing.status  # type: ignore[return-value]

    # ── Plan entitlement check ─────────────────────────────────────────────────
    # Maps automation_type → required feature_key from plan_entitlements.py.
    #
    # Rules (matches authoritative Feature Map):
    #   Starter:   order_confirmed, order_notifications, shipping/tracking — NO lock
    #   Growth+:   full autopilot, cart_recovery_stage_3, growth engine, offers
    #   Scale+:    store_brain_advanced, escalation_rules
    #
    # automation_type values come from SmartAutomation.automation_type column.
    _AUTOMATION_FEATURE_MAP: Dict[str, str] = {
        # ── Cart recovery stage 3 (Growth+) ───────────────────────────────────
        "cart_recovery_stage_3":         "cart_recovery_stage_3",
        "abandoned_cart_stage_3":        "cart_recovery_stage_3",
        "abandoned_cart_recovery_stage_3": "cart_recovery_stage_3",

        # ── Full autopilot (Growth+) ───────────────────────────────────────────
        "customer_winback":              "autopilot_customer_recovery",
        "cod_confirmation":              "autopilot_cod_confirmation",

        # ── Growth engine (Growth+) ────────────────────────────────────────────
        "predictive_reorder":            "predictive_reorder",
        "vip_upgrade":                   "vip_rewards",
        "back_in_stock":                 "back_in_stock_alerts",
        "new_product_alert":             "new_products_alerts",

        # ── Offers (Growth+) ──────────────────────────────────────────────────
        "seasonal_offer":                "seasonal_smart_offers",
        "salary_payday_offer":           "salary_offers",
        "national_day_offer":            "seasonal_smart_offers",
        "ramadan_offer":                 "seasonal_smart_offers",

        # ── Conversion tools (Growth+) ────────────────────────────────────────
        "smart_discount_popup":          "smart_discount_popup",

        # ── Scale-only ────────────────────────────────────────────────────────
        "escalation_rule":               "escalation_rules",
        "advanced_discount_rule":        "advanced_discount_rules",
    }

    _atype = getattr(automation, "automation_type", "") or ""
    _required_feature = _AUTOMATION_FEATURE_MAP.get(_atype)

    if _required_feature:
        try:
            from core.plan_entitlements import get_entitlements as _get_ent  # noqa: PLC0415
            _ent = _get_ent(db, tenant_id)
            if not _ent.has_feature(_required_feature):
                _write_execution(
                    db, event.id, automation.id, event.customer_id, tenant_id,
                    status="skipped",
                    skip_reason=f"plan_locked:{_required_feature}:{_ent.plan_slug}",
                )
                logger.info(
                    "[AutoEngine] Plan lock — automation=%s type=%s feature=%s plan=%s tenant=%s",
                    automation.id, _atype, _required_feature, _ent.plan_slug, tenant_id,
                )
                return "skipped"
        except Exception as _ent_exc:
            logger.debug("[AutoEngine] entitlement check non-fatal: %s", _ent_exc)

    # ── Delay check ───────────────────────────────────────────────────────────
    config: Dict[str, Any] = automation.config or {}
    delay_minutes: int = _resolve_delay(config, event=event)
    event_age_minutes = (now - _naive_utc(event.created_at)).total_seconds() / 60.0
    if event_age_minutes < delay_minutes:
        remaining = delay_minutes - event_age_minutes
        logger.debug(
            "[AutoEngine] Delay not elapsed event=%s automation=%s remaining=%.1f min",
            event.id, automation.id, remaining,
        )
        return "delay"

    # ── Opt-out / unsubscribe guard ───────────────────────────────────────────
    # Check BEFORE running the automation — an unsubscribed customer (or one
    # currently in pending-confirmation state) must not receive ANY message
    # regardless of automation type or stage.
    if event.customer_id:
        try:
            from models import Customer as _Customer  # noqa: PLC0415
            from services.unsubscribe import (  # noqa: PLC0415
                expire_pending_if_needed as _expire_pending,
                is_silenced as _is_silenced,
            )
            _cust = db.query(_Customer).filter(
                _Customer.id == event.customer_id,
                _Customer.tenant_id == tenant_id,
            ).first()
            if _cust:
                _expire_pending(db, _cust, commit=True)
            if _cust and _is_silenced(_cust):
                _meta = getattr(_cust, "extra_metadata", None) or {}
                _reason = (
                    "customer_unsubscribed"
                    if _meta.get("is_unsubscribed")
                    else "customer_pending_unsubscribe"
                )
                _write_execution(
                    db, event.id, automation.id, event.customer_id, tenant_id,
                    status="skipped", skip_reason=_reason,
                )
                logger.info(
                    "[AutoEngine] Skipping automation event=%s — customer %s state=%s",
                    event.id, event.customer_id, _reason,
                )
                return "skipped"
        except Exception as _unsub_exc:
            logger.warning("[AutoEngine] Opt-out check failed: %s", _unsub_exc)

    # ── Saudi quiet-hours guard ──────────────────────────────────────────────
    # When the merchant has enabled Saudi quiet hours (default ON for the
    # cart-recovery automation), a stage that becomes due between 00:00
    # and 08:00 KSA is held back until 08:30 KSA on the same day. We
    # short-circuit with `delay` so the next engine cycle re-checks; this
    # is cheaper than scheduling a one-off wakeup and keeps the engine's
    # crash-safety story intact (no in-memory timers).
    if config.get("respect_saudi_quiet_hours", False):
        from core.saudi_time_guard import (  # noqa: PLC0415
            adjust_for_saudi_sleep_window,
            is_inside_quiet_hours,
        )
        if is_inside_quiet_hours(now):
            adjusted = adjust_for_saudi_sleep_window(now)
            logger.info(
                "[AutoEngine] Saudi quiet hours — deferring event=%s automation=%s "
                "until %s",
                event.id, automation.id, adjusted.isoformat(),
            )
            return "delay"

    # ── Condition evaluation ──────────────────────────────────────────────────
    from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415

    passed, skip_reason = _evaluate_conditions(db, event, config)
    if not passed:
        _write_execution(
            db, event.id, automation.id, event.customer_id, tenant_id,
            status="skipped", skip_reason=skip_reason,
        )
        _log_event(
            _EVENTS.AUTOMATION_EXECUTION_SKIPPED,
            tenant_id=tenant_id,
            event_id=event.id,
            event_type=event.event_type,
            automation_id=automation.id,
            customer_id=event.customer_id,
            reason=skip_reason,
        )
        return "skipped"

    # ── Global Send Governor ──────────────────────────────────────────────────
    # يتحقق من الأولوية، حدود الإرسال، cooldown، وإلغاء الاشتراك.
    # يعمل فقط عندما يوجد customer_id — الأحداث بدون عميل تمر مباشرة.
    #
    # قانون السلوك (راجع send_governor.py → GOVERNOR DECISION LAW):
    #   ALLOW_SEND  → كمل إلى _execute_action
    #   SOFT_BLOCK  → أعد "delay"  — لا تكتب execution record أبداً
    #   HARD_BLOCK  → اكتب execution(skipped) ثم أعد "skipped"
    if event.customer_id:
        try:
            from core.send_governor import (  # noqa: PLC0415
                GovernorDecisionType as _GDT,
                check as _gov_check,
            )
            _order_id = (getattr(event, "payload", None) or {}).get("order_id")
            _gov = _gov_check(
                db, tenant_id, event.customer_id,
                automation.automation_type,
                order_id=_order_id,
            )

            if _gov.decision_type != _GDT.ALLOW_SEND:
                logger.info(
                    "[AutoEngine] Governor decision=%s event=%s automation=%s "
                    "customer=%s reason=%s label=%r",
                    _gov.decision_type.value,
                    event.id, automation.id, event.customer_id,
                    _gov.reason_code, _gov.label_ar,
                )

                if _gov.decision_type == _GDT.HARD_BLOCK:
                    # منع دائم: سجّل execution record → idempotency تمنع الإعادة
                    _write_execution(
                        db, event.id, automation.id, event.customer_id, tenant_id,
                        status="skipped",
                        skip_reason=_gov.reason_code,  # لن يكون SOFT_BLOCK_REASON
                        action_taken={
                            "governor":      True,
                            "decision_type": _gov.decision_type.value,
                            "label_ar":      _gov.label_ar,
                            "suggestion_ar": _gov.suggestion_ar,
                            "blocked_by":    _gov.blocked_by_type,
                        },
                    )
                    return "skipped"

                # SOFT_BLOCK: لا تكتب execution record — لتبقى idempotency سليمة
                # event.processed يبقى False → يُعاد تقييمه في الدورة القادمة
                # _write_execution() ستُطلق RuntimeError لو حاول أحد كتابته خطأً
                return "delay"

        except RuntimeError:
            raise  # أعد إطلاق أخطاء Guard الصريحة (لا تبتلعها)
        except Exception as _gov_exc:
            logger.warning("[AutoEngine] Governor check failed (non-fatal): %s", _gov_exc)

    # ── Execute action ────────────────────────────────────────────────────────
    success, action_info = await _execute_action(db, tenant_id, event, automation, config)

    # ── Result classification ────────────────────────────────────────────────
    # Three outcomes — not two — so the dashboard can distinguish a real
    # send failure (red badge, retry button) from a deliberate skip
    # (amber, "تم التخطّي because customer purchased / opted out").
    # Conversion-layer pre-send guards return (False, {"skipped": True,
    # "skip_reason": "..."}) and we want those to land as
    # status="skipped", not "failed" with a NULL error_message — that
    # latter combo is what made the queue show "فشل الإرسال" with no
    # explanation for converted carts.
    if success:
        status = "sent"
        error_message = None
    elif action_info.get("skipped") and action_info.get("skip_reason"):
        status = "skipped"
        error_message = None
    else:
        status = "failed"
        # Persist the Arabic UX label (when classified) so the dashboard
        # renders a human-readable reason. The raw English code stays
        # available on ``action_taken['error_code']`` / ``['error']`` for
        # filtering / analytics.
        error_message = (
            action_info.get("error_label")
            or action_info.get("error")
            or action_info.get("error_code")
        )

    # On failure we now ALSO persist ``action_info`` (template name, to,
    # meta_error envelope, error_code, error_label) so the per-cart
    # recovery timeline can render a useful diagnosis without a second
    # round-trip — and the manual-retry endpoint has the context it
    # needs to re-enqueue with the same step_idx.
    _exec_id = _write_execution(
        db, event.id, automation.id, event.customer_id, tenant_id,
        status=status,
        action_taken=action_info if action_info else None,
        skip_reason=action_info.get("skip_reason") if status == "skipped" else None,
        error_message=error_message,
    )

    if success:
        automation.stats_triggered = (automation.stats_triggered or 0) + 1
        automation.stats_sent = (automation.stats_sent or 0) + 1
        automation.updated_at = _utcnow_naive()
        _log_event(
            _EVENTS.AUTOMATION_EXECUTION_SENT,
            tenant_id=tenant_id,
            event_id=event.id,
            event_type=event.event_type,
            automation_id=automation.id,
            customer_id=event.customer_id,
            template=action_info.get("template"),
            wa_message_id=action_info.get("wa_message_id"),
        )
        # سجّل في Governor حتى تُحسب الحدود صحيحاً في الدورات القادمة.
        # _write_execution أعاد الـ PK بعد flush مباشرة — لا query ثانية.
        if event.customer_id:
            try:
                from core.send_governor import record_sent as _gov_record  # noqa: PLC0415
                _gov_record(
                    db, tenant_id, event.customer_id,
                    automation.automation_type,
                    execution_id=_exec_id,   # PK من _write_execution أعلاه
                )
            except Exception as _rec_exc:
                logger.warning("[AutoEngine] governor record_sent failed (non-fatal): %s", _rec_exc)
    else:
        _log_event(
            _EVENTS.AUTOMATION_EXECUTION_FAILED,
            level=logging.ERROR,
            tenant_id=tenant_id,
            event_id=event.id,
            event_type=event.event_type,
            automation_id=automation.id,
            customer_id=event.customer_id,
            reason=action_info.get("error"),
        )
    return status


# ── Internal: helpers ─────────────────────────────────────────────────────────

def _resolve_delay(config: Dict[str, Any], *, event: Any = None) -> int:
    """
    Extract delay_minutes from automation config (flat or steps-based).

    When an `event` is passed and its `payload.step_idx` is greater than
    zero, the event is a follow-up that was emitted by a sweeper after
    the configured delay had already elapsed on the parent event — it
    must NOT wait the stage-1 delay again. Returning 0 lets the engine
    process the follow-up immediately.
    """
    if event is not None:
        try:
            payload = getattr(event, "payload", None) or {}
            if payload.get("manual_retry"):
                return 0
            step_idx = int(payload.get("step_idx") or 0)
            if step_idx > 0:
                return 0
        except Exception:
            pass
    # Flat form: {"delay_minutes": 30}
    if "delay_minutes" in config:
        return int(config["delay_minutes"])
    # Steps form: {"steps": [{"delay_minutes": 30, ...}, ...]}
    steps = config.get("steps") or []
    if steps and isinstance(steps[0], dict):
        return int(steps[0].get("delay_minutes", 0))
    return 0


def _evaluate_conditions(
    db: Session, event: Any, config: Dict[str, Any]
) -> Tuple[bool, Optional[str]]:
    """
    Evaluate automation conditions against the event's customer profile.
    Returns (passed, skip_reason).
    """
    conditions: Dict[str, Any] = config.get("conditions") or {}
    if not conditions:
        return True, None

    customer_id = event.customer_id
    if not customer_id:
        return False, "no_customer_id"

    from models import CustomerProfile  # noqa: PLC0415

    profile: Optional[Any] = (
        db.query(CustomerProfile)
        .filter(CustomerProfile.customer_id == customer_id)
        .first()
    )
    if not profile:
        return False, "no_customer_profile"

    # customer_status must be in the allowed list
    allowed_statuses: List[str] = conditions.get("customer_status") or []
    if allowed_statuses:
        current_status = profile.customer_status or profile.segment or ""
        if current_status not in allowed_statuses:
            return False, f"customer_status={current_status} not in {allowed_statuses}"

    # minimum lifetime spend
    min_spent = conditions.get("min_spent_sar")
    if min_spent is not None:
        actual_spent = float(getattr(profile, "total_spend_sar", 0) or 0)
        if actual_spent < float(min_spent):
            return False, f"total_spend_sar={actual_spent} < min_spent={min_spent}"

    # RFM segment
    allowed_rfm: List[str] = conditions.get("rfm_segment") or []
    if allowed_rfm:
        current_rfm = getattr(profile, "rfm_segment", None) or ""
        if current_rfm not in allowed_rfm:
            return False, f"rfm_segment={current_rfm} not in {allowed_rfm}"

    # event payload conditions (arbitrary key→value checks)
    payload_conds: Dict[str, Any] = conditions.get("payload") or {}
    for key, expected in payload_conds.items():
        actual = (event.payload or {}).get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False, f"payload.{key}={actual} not in {expected}"
        elif actual != expected:
            return False, f"payload.{key}={actual} != {expected}"

    return True, None


async def _execute_action(
    db: Session,
    tenant_id: int,
    event: Any,
    automation: Any,
    config: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Send a WhatsApp message to the event's customer.

    Per-step `delivery_mode` controls the wire format:
      • "template"     — Meta-approved template send (opens marketing
                         conversation if no service window is open).
                         Default and the only legal mode for stage 1.
      • "interactive"  — Free-form interactive message with dynamic reply
                         buttons. Used for stages 2-4 of the cart-recovery
                         workflow when the customer service window is
                         still open. Falls back to template delivery if
                         the window has closed.
      • "ai_recovery"  — Optional Claude-driven recovery turn. Records a
                         skip when the merchant hasn't enabled it.

    Returns (success, info_dict).
    """
    from models import Customer, WhatsAppConnection, WhatsAppTemplate  # noqa: PLC0415
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    # ── Outbound billing guard ────────────────────────────────────────────────
    # Inbound events, sync, and analytics are ALWAYS allowed (see process_pending_events).
    # This is the ONLY place where we gate outbound WhatsApp sends.
    # Trials, active subscriptions, and Salla paid plans all pass.
    # trial_blocked / cancelled / failed → skip with billing_access_denied reason.
    from core.billing import has_billing_access as _has_access  # noqa: PLC0415
    if not _has_access(db, tenant_id):
        return False, {
            "error":       "billing_access_denied",
            "error_code":  "billing_access_denied",
            "error_label": "الاشتراك منتهٍ أو التجربة مستخدمة — لا يمكن إرسال رسائل",
        }

    from core.wa_usage import check_limit as _check_quota  # noqa: PLC0415

    _quota = _check_quota(db, tenant_id, category="marketing")
    if not _quota.allowed:
        return False, {
            "error":       _quota.reason,
            "error_code":  _quota.reason,
            "error_label": (
                f"تم تجاوز حد المحادثات الشهري ({_quota.used_total}/{_quota.limit})"
            ),
            "quota_used":  _quota.used_total,
            "quota_limit": _quota.limit,
        }

    customer_id = event.customer_id
    if not customer_id:
        return False, {
            "error":       "no_customer_id",
            "error_code":  "no_customer",
            "error_label": "العميل غير مرتبط بالحدث",
        }

    # ── Customer + phone ──────────────────────────────────────────────────────
    customer: Optional[Any] = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .first()
    )
    if not customer or not customer.phone:
        return False, {
            "error":       "no_customer_phone",
            "error_code":  "missing_phone_number",
            "error_label": "لا يوجد رقم جوال للعميل",
        }

    # P0 fix: never silently fall back to the raw `customer.phone` when
    # normalisation fails. Pre-fix this path sent a non-E.164 string to
    # Meta and the Cloud API either rejected it inline (empty
    # ``messages`` array) or accepted it and dropped the message later
    # at delivery time — both surfaced as a generic "send failed" with
    # no diagnostic value. Now we short-circuit with a structured
    # ``invalid_phone_number`` so the dashboard can show the real cause
    # and the merchant knows to fix the customer record.
    normalized_phone = normalize_phone(customer.phone)
    if not normalized_phone:
        return False, {
            "error":       "invalid_phone_number",
            "error_code":  "invalid_phone_number",
            "error_label": "رقم الجوال غير صالح",
            "raw_phone":   str(customer.phone)[:64],
        }
    to_phone = normalized_phone

    # ── WhatsApp connection ───────────────────────────────────────────────────
    wa_conn: Optional[Any] = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status == "connected",
        )
        .first()
    )
    if not wa_conn:
        return False, {
            "error":       "no_whatsapp_connection",
            "error_code":  "no_whatsapp_connection",
            "error_label": "لم يتم ربط واتساب الأعمال",
        }

    # ── Per-step delivery routing (cart-recovery workflow) ───────────────────
    # Resolve the active step once and let it decide the wire format. The
    # `template` branch below is preserved for everything else.
    active_step: Dict[str, Any] = _active_step_for_event(event, config)
    if not active_step.get("enabled", True) and active_step:
        return False, {
            "error":       "step_disabled",
            "error_code":  "step_disabled",
            "error_label": "هذه المرحلة معطّلة في الإعدادات",
        }

    # ── Conversion Layer (WHAT to send, before we pick WHEN) ─────────────────
    # Only the cart-recovery workflow gets the layer today — every other
    # automation keeps the legacy direct-to-template path. That boundary
    # is what lets us roll this out without churning unrelated flows.
    conversion_decision = None
    is_cart_recovery = (
        getattr(automation, "automation_type", None) == "abandoned_cart"
    )
    if is_cart_recovery:
        # ── P0 fast-path pre-send guard ──────────────────────────────────
        # If the order webhook (or a manual cancel) has already stamped
        # ``recovery_converted_at`` on this event — or on the parent
        # event for a follow-up — never send. This is a layered defence
        # on top of the conversion-layer DB lookup below: even if the
        # Order rows haven't synced or the lookup misses, the explicit
        # cancel stamp is the source of truth and we honour it directly.
        skip_reason_fast = _detect_recovery_already_converted(db, event)
        if skip_reason_fast:
            logger.info(
                "[AutoEngine] cart-recovery pre-send guard fired "
                "tenant=%s automation=%s event=%s reason=%s — skipping "
                "step because customer has already purchased.",
                tenant_id, getattr(automation, "id", None),
                getattr(event, "id", None), skip_reason_fast,
            )
            return False, {
                "skipped":     True,
                "skip_reason": skip_reason_fast,
            }

        try:
            from services.conversion_layer import (  # noqa: PLC0415
                build_context as _conv_build_context,
                decide as _conv_decide,
            )
            ctx = _conv_build_context(
                db,
                tenant_id=tenant_id, event=event, customer=customer,
                automation=automation, active_step=active_step, config=config,
            )
            conversion_decision = _conv_decide(
                ctx, active_step=active_step, config=config,
            )
        except Exception as exc:
            logger.exception(
                "[AutoEngine] Conversion layer failed — falling back to "
                "step config. tenant=%s automation=%s event=%s: %s",
                tenant_id, getattr(automation, "id", None),
                getattr(event, "id", None), exc,
            )
            conversion_decision = None

        if conversion_decision is not None and not conversion_decision.proceed:
            # Optional reschedule — re-queue this step later instead of
            # killing it outright (e.g. customer is actively chatting).
            if conversion_decision.reschedule_minutes > 0:
                try:
                    _reschedule_followup_event(
                        db, tenant_id=tenant_id, event=event,
                        step_idx=active_step_index_for_event(event),
                        delay_minutes=conversion_decision.reschedule_minutes,
                        reason=conversion_decision.skip_reason or "rescheduled",
                    )
                except Exception as exc:
                    logger.warning(
                        "[AutoEngine] Reschedule failed tenant=%s event=%s: %s",
                        tenant_id, getattr(event, "id", None), exc,
                    )
            info: Dict[str, Any] = {
                "skipped":      True,
                "skip_reason":  conversion_decision.skip_reason,
                "reschedule_minutes": conversion_decision.reschedule_minutes,
                "conversion_audit": conversion_decision.audit,
            }
            return False, info

    # ── Delivery policy resolution ───────────────────────────────────────────
    from core.wa_usage import has_open_service_window  # noqa: PLC0415
    from services.delivery_policy import resolve_delivery_mode  # noqa: PLC0415

    window_open = has_open_service_window(db, tenant_id, to_phone)

    # ── Cart recovery: stop follow-up stages if the customer replied ──────
    # When the customer replies, the conversation is handed to AI/human
    # support. Template-based reminders should stop so the customer is
    # not bothered while already in a live conversation.
    if is_cart_recovery and window_open:
        step_idx = int((getattr(event, "payload", None) or {}).get("step_idx") or 0)
        if step_idx > 0:
            logger.info(
                "[AutoEngine] cart-recovery stage %d skipped — customer "
                "replied (service window open). tenant=%s event=%s",
                step_idx, tenant_id, getattr(event, "id", None),
            )
            return False, {
                "skipped":     True,
                "skip_reason": "customer_replied",
                "step_idx":    step_idx,
            }

    ai_eligible = bool(
        (active_step.get("ai_recovery_enabled")
         or config.get("ai_recovery_enabled"))
    )

    # Cart recovery: always use template mode — no interactive/AI.
    # Templates work regardless of service window state.
    if is_cart_recovery:
        from services.delivery_policy import DeliveryDecision  # noqa: PLC0415
        decision = DeliveryDecision(
            mode="template", reason="cart_recovery_template_only",
            primary="template", fallback="template",
            used_fallback=False, window_open=window_open,
            ai_eligible=False,
        )
    else:
        override = (
            conversion_decision.delivery_mode_override
            if conversion_decision else None
        )
        if override:
            decision = resolve_delivery_mode(
                step={"primary_mode": override,
                      "fallback_mode": active_step.get("fallback_mode")
                                       or config.get("fallback_mode") or "template",
                      "ai_recovery_enabled": ai_eligible},
                config=config,
                window_open=window_open,
                ai_eligible=ai_eligible,
            )
        else:
            decision = resolve_delivery_mode(
                step=active_step, config=config,
                window_open=window_open, ai_eligible=ai_eligible,
            )

    delivery_mode = decision.mode
    logger.info(
        "[AutoEngine] tenant=%s event=%s delivery_policy primary=%s "
        "fallback=%s window_open=%s ai_eligible=%s -> mode=%s reason=%s",
        tenant_id, getattr(event, "id", None),
        decision.primary, decision.fallback,
        decision.window_open, decision.ai_eligible,
        decision.mode, decision.reason,
    )

    if delivery_mode == "ai_recovery":
        # The policy module already verified ai_eligible; this is just
        # belt-and-braces in case future call sites bypass it.
        if not ai_eligible:
            return False, {
                "error":           "ai_recovery_disabled",
                "error_code":      "ai_recovery_disabled",
                "error_label":     "خطوة الذكاء الاصطناعي غير مفعّلة",
                "delivery_policy": decision.to_audit(),
            }
        return await _execute_ai_recovery_step(
            db, tenant_id=tenant_id, event=event, customer=customer,
            wa_conn=wa_conn, to_phone=to_phone, config=config,
            active_step=active_step, automation_id=getattr(automation, "id", None),
        )

    if delivery_mode == "interactive":
        return await _execute_interactive_step(
            db, tenant_id=tenant_id, event=event, customer=customer,
            wa_conn=wa_conn, to_phone=to_phone, config=config,
            active_step=active_step, automation=automation,
            conversion_decision=conversion_decision,
        )
    # Anything else falls through to the template path below.

    # ── Template lookup ───────────────────────────────────────────────────────
    #
    # Resolution priority (first match wins):
    #   1. Service-key + step-number resolver  — the canonical path.
    #      Uses the merchant's explicitly-active template binding.
    #   2. Per-step template_name from the automation config (legacy).
    #   3. Automation-wide template_id FK (legacy).
    #   4. Automation-wide template_name from config (legacy).
    #
    # Path 1 respects the single-active invariant and the session-window
    # rule: templates are only needed when the 24h window is CLOSED.
    # (When the window is open the code path above already branched to
    #  interactive/AI mode via resolve_delivery_mode.)

    template: Optional[Any] = None

    # Path 1 — service-key SMART resolver (preferred). Uses the merchant's
    # explicit binding when present, otherwise walks a fallback chain
    # (matching service_key, nahla_source_key, config template_name) and
    # AUTO-BINDS the first APPROVED template that plausibly serves the
    # slot. See `core.service_template_resolver.resolve_template_for_send`
    # for the full chain.
    #
    # Critical: derive `service_key` and `step_number` from
    # automation_type / step position when the seed config doesn't carry
    # them explicitly. The cart_abandoned automation seed historically
    # stored only `template_name` per step, which meant the smart
    # resolver was NEVER invoked for cart-recovery sends — the engine
    # fell straight through to a strict name lookup that would only
    # match the seed's literal `abandoned_cart_recovery_ar`. Real
    # merchant templates (`nahla_abandoned_cart_reminder_<rand>`) never
    # matched, producing `template_not_approved` even when an APPROVED
    # template was right there. This derivation is what guarantees the
    # smart resolver runs for every multi-step automation.
    svc_key  = (
        active_step.get("service_key")
        or config.get("service_key")
        or _derive_service_key(automation, active_step)
    )
    step_num = active_step.get("step_number")
    if step_num is None:
        step_num = _derive_step_number(active_step, config)
    tpl_name = (
        active_step.get("template_name")
        or config.get("template_name")
    )

    preferred_source_key = (
        (getattr(event, "payload", None) or {}).get("nahla_source_key")
        or config.get("nahla_source_key")
        or active_step.get("nahla_source_key")
    )
    if preferred_source_key and svc_key:
        template = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.service_key == svc_key,
                WhatsAppTemplate.nahla_source_key == str(preferred_source_key),
                WhatsAppTemplate.status == "APPROVED",
            )
            .order_by(
                WhatsAppTemplate.is_active.desc(),
                WhatsAppTemplate.updated_at.desc(),
            )
            .first()
        )
        if template:
            logger.info(
                "[AutoEngine] Template resolved via nahla_source_key=%s service=%s → id=%s name=%s",
                preferred_source_key, svc_key, template.id, template.name,
            )

    # Single-step services (payment_reminder, cod_confirmation, vip_exclusive,
    # welcome_message, new_arrivals, …) have no per-step config and resolve to
    # `step_num=None`.  The resolver / DB use `step_number IS NULL` for the
    # canonical row in that case; previously this branch required
    # `step_num is not None` and silently bypassed the smart resolver for
    # every single-step automation, causing template_not_approved /
    # template_param_mismatch errors on otherwise-APPROVED templates.
    if svc_key:
        try:
            from core.service_template_resolver import resolve_template_for_send  # noqa: PLC0415
            _step_for_resolver = int(step_num) if step_num is not None else None
            if not template:
                template = resolve_template_for_send(
                    db, tenant_id, svc_key, _step_for_resolver,
                    fallback_template_name=tpl_name,
                )
            if template:
                logger.info(
                    "[AutoEngine] Template resolved via service_key=%s step=%s → id=%s name=%s",
                    svc_key, step_num, template.id, template.name,
                )
            else:
                logger.info(
                    "[AutoEngine] No template resolved for service_key=%s step=%s "
                    "(tenant=%s, fallback_name=%r) — see ServiceResolver MISS logs",
                    svc_key, step_num, tenant_id, tpl_name,
                )
        except Exception as exc:
            logger.warning("[AutoEngine] service_template_resolver error: %s", exc)

    # Path 2 — automation-wide template_id FK (legacy fallback)
    if not template and not tpl_name and automation.template_id:
        template = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.id == automation.template_id,
                WhatsAppTemplate.status == "APPROVED",
            )
            .first()
        )

    # Path 3 — config-level template_name (used when no service_key
    # binding exists at all, e.g. older automation rows).
    if not template and tpl_name:
        template = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.name == tpl_name,
                WhatsAppTemplate.status == "APPROVED",
            )
            .first()
        )

    if not template:
        # Distinguish "no template at all" from "template exists but
        # has no APPROVED variant", so the dashboard can guide the
        # merchant to the right action (import vs submit-for-approval
        # vs activate). We look at ANY template the merchant has on
        # this slot — even REJECTED / PENDING — to figure out which
        # state we're in.
        any_for_slot = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.service_key == svc_key,
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .first()
            if svc_key else None
        )
        any_by_name = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.name == tpl_name,
            )
            .first()
            if tpl_name else None
        )
        candidate = any_for_slot or any_by_name
        if candidate is not None:
            cand_status = (candidate.status or "").upper()
            if cand_status == "APPROVED":
                # Approved exists somewhere but couldn't be matched to
                # this slot even with the smart resolver — surface a
                # distinct hint so the merchant can fix the binding.
                error_label = (
                    "يوجد قالب معتمد لكنه غير مربوط بهذه المرحلة. "
                    "افتح صفحة القوالب وفعّل القالب المناسب لخدمة "
                    f"«{svc_key or 'الإرسال'}» للمرحلة {step_num}."
                )
            elif cand_status in {"PENDING", "IN_REVIEW", "SUBMITTED"}:
                error_label = (
                    "القالب لم يُعتمد من Meta بعد — قيد المراجعة. "
                    "سيُستأنف الإرسال تلقائياً عند الاعتماد."
                )
            elif cand_status == "REJECTED":
                error_label = (
                    "Meta رفض القالب. عدّل النص وأعد التقديم من صفحة القوالب."
                )
            else:
                error_label = "القالب غير معتمد من Meta"
        else:
            error_label = (
                "لا يوجد قالب لهذه المرحلة. استورد قالباً من مكتبة نحلة "
                "أو أنشئه من صفحة القوالب ثم قدّمه للاعتماد."
            )
        return False, {
            "error":       "no_approved_template",
            "error_code":  "template_not_approved",
            "error_label": error_label,
            "template":    tpl_name or svc_key or (str(automation.template_id) if automation.template_id else None),
            "service_key": svc_key,
            "step_number": step_num,
        }

    # ── Auto-coupon resolution ───────────────────────────────────────────────
    # When the automation step opts in via `auto_coupon: true` (e.g.
    # cart_abandoned reminder #3 or vip_customer_upgrade), we pull a real
    # discount code from the merchant's Salla-synced coupon pool and feed
    # it into the named-slot resolver below as `discount_code` / `vip_coupon`.
    # Any failure here is non-fatal — we log a structured event and let the
    # template render with an empty coupon slot rather than block the send.
    # Pass the automation_type alongside config so the rule lookup can
    # find the matching merchant-edited rule from the Coupons page.
    _config_with_type = dict(config or {})
    _config_with_type.setdefault("automation_type", getattr(automation, "automation_type", None))
    coupon_extras = await _resolve_auto_coupon(
        db, tenant_id=tenant_id, customer=customer, config=_config_with_type,
        active_step=active_step,
        automation_id=getattr(automation, "id", None),
        event_id=getattr(event, "id", None),
    )

    _store_name_resolved = _resolve_store_name(db, tenant_id)

    # ── Build template variables ──────────────────────────────────────────────
    vars_map = _build_template_vars(
        event, customer, config,
        template_name=template.name,
        store_name=_store_name_resolved,
        coupon_extras=coupon_extras,
    )

    # ── Count placeholders in EVERY component (BODY/HEADER/BUTTONS) ──────────
    # Meta's parameter count must EXACTLY match the approved template, per
    # component, otherwise it returns 132000 / template_param_mismatch.
    # The local `template.components` mirror the live Meta-approved structure
    # because /templates/sync overwrites them on every cycle.
    import re as _re
    _PLACEHOLDER_RE = _re.compile(r"\{\{[^{}]+\}\}")

    def _ph_count(text: Any) -> int:
        return len(_PLACEHOLDER_RE.findall(str(text or "")))

    _body_ph_count = 0
    _header_ph_count = 0
    _header_format = ""
    _button_ph_count = 0  # number of BUTTON components Meta expects (URL+COPY_CODE)
    for _tcomp in (template.components or []):
        _ttype = str(_tcomp.get("type", "")).upper()
        if _ttype == "BODY":
            _body_ph_count = _ph_count(_tcomp.get("text"))
        elif _ttype == "HEADER":
            _header_format = str(_tcomp.get("format", "")).upper()
            if _header_format == "TEXT":
                _header_ph_count = _ph_count(_tcomp.get("text"))
        elif _ttype == "BUTTONS":
            for _btn in (_tcomp.get("buttons") or []):
                _btype = str(_btn.get("type", "")).upper()
                if _btype == "COPY_CODE":
                    _button_ph_count += 1
                elif _btype == "URL" and "{{" in str(_btn.get("url", "") or ""):
                    _button_ph_count += 1

    # ── BODY parameters: pad/trim to EXACTLY body_ph_count ───────────────────
    # When the template is NOT in the library (e.g. tenant-scoped names like
    # nahla_payment_reminder_a653), _build_template_vars returns a 2-var
    # positional fallback. If the stored components reveal _body_ph_count > 2,
    # we need to fill the extra slots with meaningful values, not just spaces.
    # The rich event payload is used to fill gaps in order: customer_name,
    # order_number, store_name, amount, payment_url, coupon_code.
    _payload_rich = dict(event.payload or {})
    _rich_fallback_values = [
        # slot 0 → customer_name (always available). Use the central
        # fallback ``"عميلنا الغالي"`` so every automation/template
        # speaks with the same voice — see ``core.customer_display``.
        (
            (getattr(customer, "name", None) or "").strip()
            or "عميلنا الغالي"
        ),
        # slot 1 → order number (very common in payment/COD templates)
        str(
            _payload_rich.get("order_number")
            or _payload_rich.get("external_order_number")
            or _payload_rich.get("order_id")
            or _payload_rich.get("external_id")
            or ""
        ),
        # slot 2 → store name
        str(_store_name_resolved or _payload_rich.get("store_name") or "متجرنا"),
        # slot 3 → amount / total
        str(
            _payload_rich.get("total")
            or _payload_rich.get("order_total")
            or _payload_rich.get("amount")
            or _payload_rich.get("cart_total")
            or ""
        ),
        # slot 4 → payment / checkout URL (body fallback when no URL button)
        str(
            _payload_rich.get("payment_url")
            or _payload_rich.get("checkout_url")
            or _payload_rich.get("cart_url")
            or ""
        ),
        # slot 5 → coupon / discount code
        str(
            coupon_extras.get("discount_code")
            or _payload_rich.get("coupon_code")
            or ""
        ),
    ]

    _var_values = list(vars_map.values())
    if _body_ph_count and len(_var_values) > _body_ph_count:
        logger.info(
            "[WA TEMPLATE BUILD] trimming body_params from %d to %d for template=%s",
            len(_var_values), _body_ph_count, template.name,
        )
        _var_values = _var_values[:_body_ph_count]
    elif _body_ph_count and len(_var_values) < _body_ph_count:
        # Pad using the rich fallback slots so template variables contain
        # meaningful content instead of spaces. This prevents template_param_mismatch
        # for tenant-scoped templates not in the library.
        _missing = _body_ph_count - len(_var_values)
        _gap_start = len(_var_values)
        _padding = []
        for _fi in range(_gap_start, _gap_start + _missing):
            _fv = _rich_fallback_values[_fi] if _fi < len(_rich_fallback_values) else " "
            _padding.append(_fv if str(_fv).strip() else " ")
        logger.warning(
            "[WA TEMPLATE BUILD] padding body_params for template=%s "
            "from %d to %d (gap filled with rich fallback values=%r)",
            template.name, len(_var_values), _body_ph_count, _padding,
        )
        _var_values = list(_var_values) + _padding

    body_params = [{"type": "text", "text": (str(v) if str(v).strip() else " ")} for v in _var_values]
    components: List[Dict[str, Any]] = []

    # ── HEADER parameters (only TEXT-format headers can carry {{N}}) ─────────
    # Meta requires {"type":"header","parameters":[{"type":"text","text":...}]}
    # whenever the approved header text contains {{N}} placeholders.
    if _header_ph_count > 0 and _header_format == "TEXT":
        # Reuse the same vars_map values; Salla/Nahla rarely needs a separate
        # header slot. We take the first N body vars by convention. If the
        # template actually has different header slots, this still avoids the
        # hard failure and the merchant can edit the rendered text downstream.
        _hdr_values = list(vars_map.values())[:_header_ph_count]
        if len(_hdr_values) < _header_ph_count:
            _hdr_values = list(_hdr_values) + [" "] * (_header_ph_count - len(_hdr_values))
        components.append({
            "type": "header",
            "parameters": [
                {"type": "text", "text": (str(v) if str(v).strip() else " ")}
                for v in _hdr_values
            ],
        })

    if body_params:
        components.append({"type": "body", "parameters": body_params})

    # ── Button parameters (URL + COPY_CODE) ──────────────────────────────────
    # Meta requires a runtime parameter component for:
    #   • Dynamic URL buttons (``{{1}}`` in the URL) — suffix text
    #   • COPY_CODE buttons — coupon_code value
    # Missing either causes Meta error 132000 / template_param_mismatch.
    _URL_SLOT_PRECEDENCE = (
        "checkout_url", "cart_url", "tracking_url", "payment_url",
        "product_url", "reorder_url", "store_url",
    )
    # Greeting-name policy (May 2026): use Customer.name verbatim and
    # only swap in the static fallback when the stored value is empty.
    # Bad names get cleaned once via the bulk "تنظيف أسماء العملاء"
    # admin tool — no runtime sanitisation in the template path.
    _customer_name_for_btn = display_name_passthrough_or_fallback(customer.name)
    _store_name_for_btn    = _store_name_resolved
    _payload_for_btn: Dict[str, Any] = dict(event.payload or {})
    _has_dynamic_url_btn = False
    _btn_suffix_resolved = False

    # Resolve coupon code once (used for COPY_CODE buttons below)
    _coupon_code_for_btn: str = (
        coupon_extras.get("coupon_code")
        or coupon_extras.get("discount_code")
        or ""
    )

    for comp in (template.components or []):
        if str(comp.get("type", "")).upper() != "BUTTONS":
            continue
        for btn_idx, btn in enumerate(comp.get("buttons", [])):
            btn_type = str(btn.get("type", "")).upper()

            # ── COPY_CODE button ──────────────────────────────────────────
            # Meta requires: {"type":"button","sub_type":"copy_code",
            #                 "index":"N","parameters":[{"type":"coupon_code",
            #                                            "coupon_code":"XYZ"}]}
            if btn_type == "COPY_CODE":
                code = _coupon_code_for_btn or "NAHLA"
                if not _coupon_code_for_btn:
                    logger.warning(
                        "[AutoEngine] COPY_CODE button on template %s has no "
                        "coupon code — using placeholder 'NAHLA'. "
                        "Enable auto_coupon on this automation to fix this.",
                        template.name,
                    )
                components.append({
                    "type":       "button",
                    "sub_type":   "copy_code",
                    "index":      str(btn_idx),
                    "parameters": [{"type": "coupon_code", "coupon_code": code}],
                })
                continue

            # ── Dynamic URL button ────────────────────────────────────────
            if btn_type != "URL":
                continue
            btn_url_tpl: str = btn.get("url", "")
            if "{{1}}" not in btn_url_tpl:
                continue
            _has_dynamic_url_btn = True
            btn_suffix = ""
            for url_slot in _URL_SLOT_PRECEDENCE:
                resolved = _resolve_slot_value(
                    slot=url_slot,
                    customer_name=_customer_name_for_btn,
                    store_name=_store_name_for_btn,
                    payload=_payload_for_btn,
                    config=config,
                    coupon_extras=coupon_extras,
                )
                if resolved:
                    btn_suffix = _extract_button_url_suffix(btn_url_tpl, resolved)
                    if btn_suffix:
                        break
            if not btn_suffix:
                # ── CRITICAL FIX: never omit the button component ──────────
                # If no URL was found in the event payload, try a chain of
                # safe fallbacks so we ALWAYS emit the button param component.
                # Skipping it entirely causes template_param_mismatch (Meta
                # error 132000) because the approved template declares {{1}}
                # in the button URL but we send 0 button-param components.
                _btn_fallback_url = str(
                    _payload_for_btn.get("payment_url")
                    or _payload_for_btn.get("checkout_url")
                    or _payload_for_btn.get("cart_url")
                    or _payload_for_btn.get("tracking_url")
                    or _payload_for_btn.get("store_url")
                    or config.get("store_url")
                    or ""
                )
                if _btn_fallback_url:
                    btn_suffix = _extract_button_url_suffix(btn_url_tpl, _btn_fallback_url)
                if not btn_suffix:
                    # Last resort: use a single space so Meta accepts the
                    # call. The button URL suffix will be empty/blank but the
                    # parameter COUNT will be correct and the send won't fail
                    # with 132000. A real URL should be populated in the event
                    # payload by the store adapter or order enrichment step.
                    btn_suffix = " "
                logger.warning(
                    "[WA TEMPLATE BUILD] Dynamic URL button on template=%s — "
                    "primary URL slots empty; using fallback suffix=%r "
                    "(tenant=%s). Populate payment_url/checkout_url in the "
                    "order record to get the real link.",
                    template.name, btn_suffix, tenant_id,
                )

            if btn_suffix:
                _btn_suffix_resolved = True
                components.append({
                    "type":       "button",
                    "sub_type":   "url",
                    "index":      str(btn_idx),
                    "parameters": [{"type": "text", "text": btn_suffix}],
                })

    send_payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"code": template.language or "ar"},
            "components": components,
        },
    }

    # ── Pre-send diagnostic ────────────────────────────────────────────────────
    # Structured log for every component so operators can instantly compare
    # against Meta's WhatsApp Manager without digging into the raw payload.
    _sent_header_params = 0
    _sent_body_params   = 0
    _sent_button_params = 0
    for _c in components:
        _ctype = str(_c.get("type", "")).lower()
        if _ctype == "header":
            _sent_header_params = len(_c.get("parameters", []) or [])
        elif _ctype == "body":
            _sent_body_params = len(_c.get("parameters", []) or [])
        elif _ctype == "button":
            _sent_button_params += 1

    _svc_key_log = svc_key or "unknown"
    logger.info(
        "[WA TEMPLATE BUILD] template=%s lang=%s service=%s | "
        "component=header expected=%d sent=%d | "
        "component=body expected=%d sent=%d | "
        "component=buttons expected=%d sent=%d",
        template.name, template.language or "ar", _svc_key_log,
        _header_ph_count, _sent_header_params,
        _body_ph_count, _sent_body_params,
        _button_ph_count, _sent_button_params,
    )

    if (
        _sent_body_params   != _body_ph_count
        or _sent_header_params != _header_ph_count
        or _sent_button_params  != _button_ph_count
    ):
        logger.error(
            "[WA TEMPLATE PARAM MISMATCH] template=%s service=%s — "
            "Meta will reject with 132000. "
            "header: expected=%d sent=%d | "
            "body: expected=%d sent=%d | "
            "buttons: expected=%d sent=%d | "
            "body_values=%r",
            template.name, _svc_key_log,
            _header_ph_count, _sent_header_params,
            _body_ph_count, _sent_body_params,
            _button_ph_count, _sent_button_params,
            [str(v)[:40] for v in _var_values],
        )

    # ── Send ──────────────────────────────────────────────────────────────────
    try:
        from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415
        from services.cart_recovery_failures import (  # noqa: PLC0415
            classify_meta_response,
            classify_send_exception,
        )

        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_template",
            phone_id=wa_conn.phone_number_id,
            payload=send_payload,
        )

        # P0 fix: Meta returns ``{"error": {...}}`` instead of
        # ``{"messages": [...]}`` whenever the template is rejected,
        # the customer can't receive, the access token expired, etc.
        # Pre-fix the engine read ``response["messages"][0]["id"]``
        # blindly and either crashed (false-failed with a Python
        # KeyError as the "reason") or silently recorded a sent row
        # with ``wa_message_id=None``. Now we classify the response
        # explicitly and surface the structured failure.
        failure = classify_meta_response(response)
        if failure is not None:
            code, label_ar, raw_meta = failure
            logger.error(
                "[AutoEngine] Template send rejected by provider "
                "tenant=%s event=%s automation=%s template=%s "
                "code=%s label=%s raw=%s | "
                "param_counts expected(body=%d header=%d buttons=%d) "
                "sent(body=%d header=%d buttons=%d)",
                tenant_id, event.id, automation.id, template.name,
                code, label_ar, raw_meta,
                _body_ph_count, _header_ph_count, _button_ph_count,
                _sent_body_params, _sent_header_params, _sent_button_params,
            )
            return False, {
                "error":       code,
                "error_code":  code,
                "error_label": label_ar,
                "meta_error":  raw_meta,
                "template":    template.name,
                "to":          to_phone,
                "vars":        vars_map,
                "param_counts": {
                    "expected": {
                        "body":    _body_ph_count,
                        "header":  _header_ph_count,
                        "buttons": _button_ph_count,
                    },
                    "sent": {
                        "body":    _sent_body_params,
                        "header":  _sent_header_params,
                        "buttons": _sent_button_params,
                    },
                },
            }

        try:
            from routers.conversations import record_outbound_message  # noqa: PLC0415
            import re as _re
            def _sub(m):
                i = int(m.group(1)) - 1
                return str(_var_values[i]) if i < len(_var_values) else m.group(0)
            _VAR_RE = _re.compile(r"\{\{(\d+)\}\}")
            _parts: list[str] = []
            for _c in (template.components or []):
                _ct = (_c.get("type") or "").upper()
                if _ct == "HEADER" and (_c.get("format") or "").upper() == "TEXT" and _c.get("text"):
                    _header = _VAR_RE.sub(_sub, _c["text"])
                    _parts.append(f"*{_header}*")
                elif _ct == "BODY" and _c.get("text"):
                    _parts.append(_VAR_RE.sub(_sub, _c["text"]))
                elif _ct == "FOOTER" and _c.get("text"):
                    _parts.append(_c["text"])
                elif _ct == "BUTTONS":
                    _bl = []
                    for _b in (_c.get("buttons") or []):
                        _bt = (_b.get("type") or "").upper()
                        _lbl = _b.get("text") or ""
                        if _bt == "COPY_CODE": _bl.append(f"📋 {_lbl or 'نسخ الكود'}")
                        elif _bt == "URL": _bl.append(f"🔗 {_lbl or 'رابط'}")
                        elif _bt == "QUICK_REPLY": _bl.append(f"↩️ {_lbl}")
                        elif _lbl: _bl.append(f"▪️ {_lbl}")
                    if _bl:
                        _parts.append("━━━━━\n" + "\n".join(_bl))
            _rendered = "\n\n".join(_parts) if _parts else f"[{template.name}]"
            record_outbound_message(
                db, tenant_id, to_phone, _rendered,
                event_type="automation",
                customer_name=customer.name or "",
                extra={"template_name": template.name, "automation_id": getattr(automation, "id", None)},
            )
        except Exception:
            pass

        action_info = {
            "template": template.name,
            "to": to_phone,
            "vars": vars_map,
            "wa_message_id": (response or {}).get("messages", [{}])[0].get("id"),
        }
        return True, action_info

    except Exception as exc:
        from services.cart_recovery_failures import classify_send_exception  # noqa: PLC0415
        code, label_ar, raw = classify_send_exception(exc)
        logger.error(
            "[AutoEngine] Send failed event=%s automation=%s tenant=%s "
            "code=%s raw=%s",
            event.id, automation.id, tenant_id, code, raw,
        )
        return False, {
            "error":       code,
            "error_code":  code,
            "error_label": label_ar,
            "exception":   raw,
            "template":    template.name,
            "to":          to_phone,
        }


def _build_template_vars(
    event: Any,
    customer: Any,
    config: Dict[str, Any],
    *,
    template_name: Optional[str] = None,
    store_name: Optional[str] = None,
    coupon_extras: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Resolve a template's `{{1}}, {{2}}, …` placeholders to real values.

    Resolution order for the var_map (which named slot each placeholder
    represents):

      1. Explicit `var_map` on the automation config (legacy contract).
      2. Default library lookup by `template_name` — this is how the 3 core
         revenue automations get their named-slot contract for free.
      3. Positional fallback `{{1}}=customer_name, {{2}}=checkout_url-or-coupon`
         to keep ad-hoc / merchant-authored templates working.

    Resolution order for each slot's *value*:

      coupon_extras > event.payload > config defaults > customer defaults.

    `coupon_extras` is the output of `_resolve_auto_coupon` — when an
    automation step opts in via `auto_coupon=True`, the engine pre-resolves
    a real coupon and passes it down here so the same code path renders
    both ad-hoc and pool-backed templates.
    """
    customer_name: str = display_name_passthrough_or_fallback(
        getattr(customer, "name", None)
    )
    payload: Dict[str, Any] = event.payload or {}
    coupon_extras = coupon_extras or {}

    # ── Determine the var_map (placeholder → named-slot) ──────────────────
    var_map: Dict[str, str] = config.get("var_map") or {}
    if not var_map and template_name:
        # Falls back to {} for non-library templates, which the positional
        # fallback below will handle.
        try:
            from core.template_library import numeric_var_map_for  # noqa: PLC0415
            var_map = numeric_var_map_for(template_name)
        except Exception:
            var_map = {}

    if var_map:
        return {
            placeholder: _resolve_slot_value(
                slot=field,
                customer_name=customer_name,
                store_name=store_name,
                payload=payload,
                config=config,
                coupon_extras=coupon_extras,
            )
            for placeholder, field in var_map.items()
        }

    # Positional fallback for templates not in the library and without an
    # explicit var_map.  The old fallback only produced 2 vars which caused
    # template_param_mismatch for any tenant-scoped template with 3+ body
    # variables (e.g. nahla_payment_reminder_a653 has 3+ vars but the name
    # doesn't match a library key so numeric_var_map_for returns {}).
    #
    # The new rich fallback produces up to 6 slots in the most common order
    # used by Nahla-generated payment/reminder templates so no space-padding
    # is needed for the typical 3-slot case.
    return {
        "{{1}}": customer_name,
        "{{2}}": str(
            payload.get("order_number")
            or payload.get("external_order_number")
            or payload.get("order_id")
            or payload.get("external_id")
            or ""
        ),
        "{{3}}": str(store_name or payload.get("store_name") or "متجرنا"),
        "{{4}}": str(
            payload.get("total")
            or payload.get("order_total")
            or payload.get("amount")
            or payload.get("cart_total")
            or ""
        ),
        "{{5}}": str(
            payload.get("payment_url")
            or payload.get("checkout_url")
            or payload.get("cart_url")
            or coupon_extras.get("discount_code")
            or coupon_extras.get("vip_coupon")
            or payload.get("coupon_code")
            or ""
        ),
        "{{6}}": str(
            coupon_extras.get("discount_code")
            or payload.get("coupon_code")
            or ""
        ),
    }


def _resolve_slot_value(
    *,
    slot: str,
    customer_name: str,
    store_name: Optional[str],
    payload: Dict[str, Any],
    config: Dict[str, Any],
    coupon_extras: Dict[str, str],
) -> str:
    """
    Single-slot resolver. Centralised so the AI rewriter, the dashboard
    preview endpoint, and the engine all agree on what each named slot
    means at send time.
    """
    if slot == "customer_name":
        return customer_name
    if slot == "store_name":
        return str(store_name or payload.get("store_name") or "متجرنا")
    if slot == "store_url":
        return str(payload.get("store_url") or config.get("store_url") or "")
    if slot == "checkout_url":
        return str(payload.get("checkout_url") or payload.get("cart_url") or "")
    if slot == "cart_url":
        return str(payload.get("cart_url") or payload.get("checkout_url") or "")
    if slot == "cart_total":
        return str(payload.get("cart_total") or payload.get("total") or "")
    if slot == "product_name":
        return str(payload.get("product_name") or "")
    if slot == "product_url":
        # Used by back_in_stock_{ar,en}. Prefer the URL the emitter
        # baked into the payload, fall back to a synthesized store URL +
        # external_id pattern when only the bare external id is known.
        url = (
            payload.get("product_url")
            or payload.get("url")
            or ""
        )
        if url:
            return str(url)
        store_url = str(payload.get("store_url") or config.get("store_url") or "").rstrip("/")
        ext = payload.get("external_id") or payload.get("product_external_id")
        if store_url and ext:
            return f"{store_url}/p/{ext}"
        return ""
    if slot == "order_id":
        # Prefer the human-facing platform number (Salla reference_id, Zid
        # code, Shopify name) which the unpaid-orders emitter writes into
        # the payload. Fall back to internal id when nothing else is known.
        return str(
            payload.get("order_number")
            or payload.get("external_order_number")
            or payload.get("order_id")
            or payload.get("external_id")
            or ""
        )
    if slot == "payment_url":
        return str(
            payload.get("payment_url")
            or payload.get("checkout_url")
            or ""
        )
    if slot == "tracking_url":
        return str(
            payload.get("tracking_url")
            or payload.get("tracking_link")
            or payload.get("shipping_tracking_url")
            or ""
        )
    if slot in ("order_total", "total"):
        return str(payload.get("total") or payload.get("order_total") or payload.get("cart_total") or "")
    if slot == "reorder_url":
        url = (
            payload.get("reorder_url")
            or payload.get("product_url")
            or payload.get("url")
            or ""
        )
        if url:
            return str(url)
        store_url = str(payload.get("store_url") or config.get("store_url") or "").rstrip("/")
        ext = payload.get("external_id") or payload.get("product_external_id")
        if store_url and ext:
            return f"{store_url}/p/{ext}"
        return store_url
    if slot == "occasion_name":
        return str(
            payload.get("occasion_name")
            or payload.get("event_name")
            or ""
        )
    if slot in ("discount_code", "coupon_code"):
        return str(
            coupon_extras.get("discount_code")
            or config.get("coupon_code")
            or payload.get("coupon_code")
            or ""
        )
    if slot == "vip_coupon":
        return str(
            coupon_extras.get("vip_coupon")
            or coupon_extras.get("discount_code")
            or config.get("coupon_code")
            or payload.get("coupon_code")
            or ""
        )
    # Unknown named slot → fall back to the raw payload key so merchant
    # extensions still work without us having to teach this resolver.
    return str(payload.get(slot, ""))


def _detect_recovery_already_converted(db: Any, event: Any) -> Optional[str]:
    """
    Cheap, allocation-free pre-send check: did the
    ``cart_recovery_cancel`` service already mark this recovery thread
    as converted?

    We look in two places:

      1. The event's own payload — covers the case where the cancel
         hook ran AFTER this event was inserted but BEFORE it was
         picked up (the most common path: order webhook lands while a
         postpone-rescheduled event sits with a future ``created_at``).

      2. The parent event's payload — for follow-up events emitted by
         ``scan_abandoned_cart_followups``, the conversion stamp lives
         on the stage-1 parent. We resolve via ``parent_event_id`` if
         present, falling back to a customer-scoped lookup of stage-1
         events for the same cart_id.

    Returns the ``recovery_cancel_reason`` string when a stamp is found
    (so the caller can record an accurate ``skip_reason`` in
    AutomationExecution), or None to fall through to the conversion
    layer's slower DB-backed check.
    """
    payload = getattr(event, "payload", None) or {}
    if payload.get("recovery_converted_at"):
        return str(payload.get("recovery_cancel_reason") or "customer_purchased")

    # Follow-up events carry parent_event_id (we set it in the postpone
    # reschedule path and the sweeper sets it implicitly via copy).
    parent_id = payload.get("parent_event_id")
    if parent_id is None:
        return None
    try:
        from models import AutomationEvent  # noqa: PLC0415
        parent = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.id == int(parent_id))
            .first()
        )
        if parent is not None:
            ppayload = parent.payload or {}
            if ppayload.get("recovery_converted_at"):
                return str(
                    ppayload.get("recovery_cancel_reason")
                    or "customer_purchased"
                )
    except Exception:
        # The fast-path is best-effort; the conversion-layer DB check
        # downstream will catch it if this lookup fails.
        return None
    return None


def active_step_index_for_event(event: Any) -> int:
    """
    Return the integer step index this event represents.

    Used by the conversion layer's reschedule path to stamp a re-queued
    event with the same step_idx it was about to run — we want the
    retry to be the same stage, just later in time.
    """
    payload = getattr(event, "payload", None) or {}
    try:
        v = payload.get("step_idx")
        if v is None:
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def _reschedule_followup_event(
    db: Session,
    *,
    tenant_id: int,
    event: Any,
    step_idx: int,
    delay_minutes: int,
    reason: str,
) -> Optional[int]:
    """
    Re-queue the current cart-recovery step to fire `delay_minutes` in
    the future instead of killing it.

    Works by inserting a new AutomationEvent with the same event_type,
    the same payload (including step_idx), and a `created_at` set to
    `now + delay_minutes`. The engine's wait-loop sees the future
    `created_at`, computes a negative age, and quietly keeps the row
    pending until the clock catches up.

    The original event is not mutated. Its execution row (written by
    the caller) records the skip reason, so the sweeper's
    already-emitted bookkeeping is preserved.

    Returns the id of the new event, or None on failure.
    """
    try:
        from models import AutomationEvent  # noqa: PLC0415
    except Exception:
        return None

    base_payload: Dict[str, Any] = dict(getattr(event, "payload", None) or {})
    base_payload["step_idx"] = int(step_idx)
    base_payload.setdefault("parent_event_id", getattr(event, "id", None))
    base_payload["reschedule_reason"] = reason
    base_payload["reschedule_source"] = "conversion_layer"

    try:
        fire_at = datetime.now(timezone.utc).replace(tzinfo=None) + \
                  timedelta(minutes=max(1, int(delay_minutes)))
        ev = AutomationEvent(
            tenant_id   = tenant_id,
            event_type  = getattr(event, "event_type", None) or "cart_abandoned",
            customer_id = getattr(event, "customer_id", None),
            payload     = base_payload,
            processed   = False,
            created_at  = fire_at,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        return ev.id
    except Exception as exc:
        logger.warning(
            "[AutoEngine] Failed to reschedule event=%s tenant=%s: %s",
            getattr(event, "id", None), tenant_id, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ── Service-key / step-number derivation ─────────────────────────────────────
#
# Maps an automation_type to the canonical `service_key` used by the
# Nahla template library and the smart resolver. This is the bridge
# that lets the resolver run even when the seed config doesn't carry
# an explicit `service_key` per step (which historically was every
# multi-step automation, including cart_abandoned).
#
# Keep in sync with `services.whatsapp_templates.nahla_templates.SERVICE_CATALOG`.
_AUTOMATION_TYPE_TO_SERVICE_KEY: Dict[str, str] = {
    "cart_abandoned":          "cart_recovery",
    "abandoned_cart":          "cart_recovery",
    "unpaid_order_reminder":   "payment_reminder",   # fix: was missing → svc_key=None bypassed smart resolver
    "abandoned_order_draft":   "wa_draft_reminder",
    "post_delivery_review":    "post_delivery",
    "cod_confirmation":        "cod_confirmation",
    "order_confirmation":      "order_confirmation",
    "shipping_update":         "shipping_update",
    "predictive_reorder":      "predictive_reorder",
    "customer_winback":        "customer_winback",
    "vip_upgrade":             "vip_customer",
    "new_product_alert":       "new_arrivals",
    "back_in_stock":           "back_in_stock",
}


def _derive_service_key(
    automation: Any,
    active_step: Dict[str, Any],
) -> Optional[str]:
    """Return the canonical service_key for an automation when the seed
    config didn't store one. Uses the automation's type as the source
    of truth — every Nahla automation maps to exactly one service slot
    family in the template library."""
    auto_type = getattr(automation, "automation_type", None)
    if not auto_type:
        return None
    return _AUTOMATION_TYPE_TO_SERVICE_KEY.get(str(auto_type))


def _derive_step_number(
    active_step: Dict[str, Any],
    config: Dict[str, Any],
) -> Optional[int]:
    """Return a 1-based step number for the current step.

    Resolution order:
      1. Explicit `step_number` already on the step (caller has already
         tried this — we guard against re-entry just in case).
      2. `step_idx` if present (0-based array index → +1).
      3. Position of `active_step` inside `config["steps"]` (+1) when
         we can identify it by object identity.
      4. None when the automation has no multi-step config (e.g.
         single-template automations like new_product_alert).
    """
    explicit = active_step.get("step_number")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass

    idx = active_step.get("step_idx")
    if idx is not None:
        try:
            return int(idx) + 1   # 0-based payload → 1-based step number
        except (TypeError, ValueError):
            pass

    steps = config.get("steps") if isinstance(config, dict) else None
    if isinstance(steps, list):
        for i, step in enumerate(steps):
            if step is active_step:
                return i + 1

    return None


def _active_step_for_event(event: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    For multi-step automations (e.g. cart_abandoned with 4 reminders), pick
    the step that should fire for this event.

    Resolution order:
      1. `payload.step_idx` — explicit index written by the sweeper. Trusted
         even when the event is brand new (age 0); the sweeper is the
         authority for which stage this row represents.
      2. Fallback: pick the latest step whose `delay_minutes` is ≤ event age.
         Kept for legacy single-event automations that don't carry a
         step_idx in the payload.

    Returns the flat config when there are no steps.
    """
    steps = config.get("steps")
    if not isinstance(steps, list) or not steps:
        return {}

    # Explicit index from the sweeper takes priority.
    payload = getattr(event, "payload", None) or {}
    explicit = payload.get("step_idx")
    if explicit is not None:
        try:
            idx = int(explicit)
            if 0 <= idx < len(steps) and isinstance(steps[idx], dict):
                return steps[idx]
        except (TypeError, ValueError):
            pass

    age_minutes = max(
        0,
        int((_utcnow_naive() - _naive_utc(event.created_at)).total_seconds() // 60),
    )
    chosen: Dict[str, Any] = steps[0]
    for step in steps:
        if not isinstance(step, dict):
            continue
        if int(step.get("delay_minutes", 0)) <= age_minutes:
            chosen = step
    return chosen


def _resolve_discount_source(
    config: Dict[str, Any],
    active_step: Dict[str, Any],
) -> str:
    """
    Determine which discount artifact this automation step wants to issue.

    Returns one of:
      'promotion' — materialise a `Coupon` row from a `Promotion` rule
      'coupon'    — pull from the segmented coupon pool (legacy default)
      'none'      — no discount; render the template with empty discount slots

    Resolution order — explicit always wins so a merchant who upgrades a
    seasonal_offer automation to point at a Promotion never accidentally
    falls back to the old auto_coupon path:

      1. active_step.discount_source     — per-step override (e.g. cart
                                            recovery stage 3 only)
      2. config.discount_source          — automation-wide setting
      3. Legacy `auto_coupon` heuristic  — config.auto_coupon /
                                            step.auto_coupon /
                                            step.message_type == 'coupon'
                                            → 'coupon' (preserves existing
                                            seeds without a migration)
      4. 'none'                          — no discount requested
    """
    explicit_step = (active_step or {}).get("discount_source")
    if explicit_step:
        return str(explicit_step)
    explicit_cfg = config.get("discount_source")
    if explicit_cfg:
        return str(explicit_cfg)

    legacy_coupon = bool(
        config.get("auto_coupon")
        or (active_step or {}).get("auto_coupon")
        or (active_step or {}).get("message_type") == "coupon"
    )
    if legacy_coupon:
        return "coupon"
    return "none"


# Per-tenant feature flag: when ON, the automation engine routes every
# discount decision through the shared OfferDecisionService so the choice
# is recorded in the ledger and obeys the merchant's frequency cap, hard
# discount cap, and signal-driven nudges. Default OFF — Phase 2 ships
# behaviourally inert; merchants opt in via the admin features API.
OFFER_DECISION_FLAG = "offer_decision_service"


def _tenant_uses_offer_decision_service(db: Session, tenant_id: int) -> bool:
    """Check `TenantSettings.extra_metadata.tenant_features.offer_decision_service`."""
    try:
        from models import TenantSettings  # noqa: PLC0415

        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts is None:
            return False
        meta = dict(ts.extra_metadata or {})
        flags = dict(meta.get("tenant_features") or {})
        return bool(flags.get(OFFER_DECISION_FLAG))
    except Exception:
        return False


async def _resolve_auto_coupon(
    db: Session,
    *,
    tenant_id: int,
    customer: Any,
    config: Dict[str, Any],
    active_step: Dict[str, Any],
    automation_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> Dict[str, str]:
    """
    Resolve the discount artifact for this automation step.

    Dispatcher that honours `discount_source` (preferred) and falls back
    to the legacy `auto_coupon` heuristic so existing seeds keep working
    with zero-touch:

      • 'promotion' → materialise a personal coupon from a Promotion rule
      • 'coupon'    → pull from the segmented coupon pool
      • 'none'      → no discount

    Returns `{"discount_code": "...", "vip_coupon": "...", "coupon_code": "..."}`
    on success (all three keys populated so cart/vip/winback templates
    consume the same code). Returns `{}` on any failure — never raises,
    never blocks the send.

    Phase 2 routing — when the per-tenant `offer_decision_service` flag is
    set, this body delegates to `OfferDecisionService.decide` +
    `apply_decision`. Behavioural parity is locked down by the seed-parity
    test suite in `tests/test_offer_decision.py`.
    """
    if _tenant_uses_offer_decision_service(db, tenant_id):
        return await _resolve_via_decision_service(
            db,
            tenant_id=tenant_id,
            customer=customer,
            config=config,
            active_step=active_step,
            automation_id=automation_id,
            event_id=event_id,
        )

    source = _resolve_discount_source(config, active_step)

    if source == "promotion":
        return await _materialise_promotion_for_send(
            db,
            tenant_id=tenant_id,
            customer=customer,
            promotion_id=config.get("promotion_id"),
        )

    if source != "coupon":
        return {}

    # Resolve segment
    segment = (
        config.get("coupon_segment")
        or _customer_segment_for(db, tenant_id, getattr(customer, "id", None))
        or "active"
    )

    # Honour the merchant-edited rule (discount value + validity window) from
    # the new editable Coupons page when an automation_type maps to a rule.
    rule_override_discount: Optional[int] = None
    rule_override_validity: Optional[int] = None
    automation_type = str(config.get("automation_type") or "") or str(active_step.get("automation_type") or "")
    if automation_type:
        try:
            from core.tenant import get_or_create_settings  # noqa: PLC0415
            from routers.coupons import get_rule_for_automation  # noqa: PLC0415

            ts = get_or_create_settings(db, tenant_id)
            rule = get_rule_for_automation(ts, automation_type)
            if rule:
                if rule.get("discount_type") == "percentage":
                    raw_pct = rule.get("discount_value")
                    if raw_pct is not None:
                        rule_override_discount = int(round(float(raw_pct)))
                vd = rule.get("validity_days")
                if isinstance(vd, (int, float)) and int(vd) > 0:
                    rule_override_validity = int(vd)
        except Exception as _exc:  # pragma: no cover — defensive only
            logger.debug("[AutoEngine] rule lookup skipped: %s", _exc)

    try:
        from services.coupon_generator import CouponGeneratorService  # noqa: PLC0415

        svc = CouponGeneratorService(db, tenant_id)
        coupon = svc.pick_coupon_for_segment(segment)
        if coupon is None:
            coupon = await svc.create_on_demand(
                segment,
                requested_discount_pct=rule_override_discount,
                validity_days_override=rule_override_validity,
            )
    except Exception as exc:
        logger.warning(
            "[AutoEngine] auto_coupon resolution failed tenant=%s segment=%s: %s",
            tenant_id, segment, exc,
        )
        return {}

    if coupon is None or not getattr(coupon, "code", None):
        from core.obs import EVENTS as _EVENTS, log_event as _log_event  # noqa: PLC0415
        _log_event(
            _EVENTS.COUPON_AUTOGEN_FAILED,
            tenant_id=tenant_id,
            customer_id=getattr(customer, "id", None),
            segment=segment,
            stage="automation_engine_auto_coupon",
            err="pool_empty_and_create_on_demand_returned_none",
        )
        return {}

    code = str(coupon.code).strip().upper()
    return {"discount_code": code, "vip_coupon": code, "coupon_code": code}


async def _materialise_promotion_for_send(
    db: Session,
    *,
    tenant_id: int,
    customer: Any,
    promotion_id: Any,
) -> Dict[str, str]:
    """
    Issue a personal coupon from a Promotion rule for this customer.

    Mirrors the return shape of `_resolve_auto_coupon`'s coupon path so
    the template renderer treats both paths identically. A missing or
    inactive promotion silently degrades to `{}` — the WhatsApp send
    proceeds without a discount slot rather than blocking on a
    misconfigured automation.
    """
    if not promotion_id:
        logger.info(
            "[AutoEngine] discount_source=promotion but no promotion_id "
            "configured tenant=%s — sending without discount",
            tenant_id,
        )
        return {}

    try:
        from services.promotion_engine import materialise_for_customer  # noqa: PLC0415

        coupon = await materialise_for_customer(
            db,
            promotion_id=int(promotion_id),
            tenant_id=tenant_id,
            customer_id=getattr(customer, "id", None),
        )
    except Exception as exc:
        logger.warning(
            "[AutoEngine] promotion materialise failed tenant=%s promo=%s: %s",
            tenant_id, promotion_id, exc,
        )
        return {}

    if coupon is None or not getattr(coupon, "code", None):
        return {}

    code = str(coupon.code).strip().upper()
    return {"discount_code": code, "vip_coupon": code, "coupon_code": code}


async def _resolve_via_decision_service(
    db: Session,
    *,
    tenant_id: int,
    customer: Any,
    config: Dict[str, Any],
    active_step: Dict[str, Any],
    automation_id: Optional[int],
    event_id: Optional[int],
) -> Dict[str, str]:
    """
    Phase-2 path: delegate to the shared `OfferDecisionService`.

    Mirrors the legacy precedence by translating the existing config keys
    into a `OfferDecisionContext`:

      • config.discount_source / step.discount_source → suggested_source
      • config.promotion_id                           → suggested_promotion_id
      • legacy `auto_coupon` heuristic                → suggested_source="coupon"
      • config.coupon_segment                         → suggested_segment

    Stamps `decision_id` onto the issued coupon (handled inside
    `apply_decision`) so order_paid attribution can join back to the
    ledger row written by `decide`.
    """
    try:
        from services.offer_decision_service import (  # noqa: PLC0415
            OfferDecisionContext,
            SOURCE_NONE,
            SURFACE_AUTOMATION,
            apply_decision,
            collect_signals,
            decide,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("[AutoEngine] decision service import failed: %s", exc)
        return {}

    legacy_source = _resolve_discount_source(config, active_step)
    suggested_source: Optional[str] = legacy_source if legacy_source != "none" else None
    suggested_promo_id = config.get("promotion_id") if legacy_source == "promotion" else None

    suggested_segment = (
        config.get("coupon_segment")
        or _customer_segment_for(db, tenant_id, getattr(customer, "id", None))
    )
    automation_type = (
        str(config.get("automation_type") or "")
        or str((active_step or {}).get("automation_type") or "")
        or None
    )

    customer_id = getattr(customer, "id", None)
    signals = collect_signals(db, tenant_id=tenant_id, customer_id=customer_id)

    ctx = OfferDecisionContext(
        tenant_id              = tenant_id,
        surface                = SURFACE_AUTOMATION,
        customer_id            = customer_id,
        automation_id          = automation_id,
        automation_type        = automation_type,
        event_id               = event_id,
        suggested_source       = suggested_source,
        suggested_promotion_id = int(suggested_promo_id) if suggested_promo_id else None,
        suggested_segment      = suggested_segment,
        signals                = signals,
    )

    decision = decide(db, ctx)
    if decision.source == SOURCE_NONE:
        return {}

    return await apply_decision(db, ctx=ctx, decision=decision, customer=customer)


def _customer_segment_for(
    db: Session, tenant_id: int, customer_id: Optional[int]
) -> Optional[str]:
    """Look up the latest customer_status / segment from CustomerProfile."""
    if not customer_id:
        return None
    try:
        from models import CustomerProfile  # noqa: PLC0415
        profile = (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id == customer_id,
            )
            .first()
        )
        if not profile:
            return None
        return getattr(profile, "customer_status", None) or getattr(profile, "segment", None)
    except Exception:
        return None


def _resolve_store_name(db: Session, tenant_id: int) -> str:
    """Return the merchant-configured store name, or a sensible default."""
    try:
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE,
            get_or_create_settings,
            merge_defaults,
        )

        settings = get_or_create_settings(db, tenant_id)
        store = merge_defaults(getattr(settings, "store_settings", None), DEFAULT_STORE)
        name = (store.get("store_name") or "").strip()
        if name:
            return name
    except Exception as exc:
        logger.debug("[AutoEngine] store_name resolution failed tenant=%s: %s", tenant_id, exc)
    return "متجرنا"


def _extract_button_url_suffix(button_url_template: str, full_url: str) -> str:
    """
    Extract the dynamic suffix that Meta expects for a URL-button parameter.

    The template stores a fixed prefix ending with ``{{1}}``
    (e.g. ``https://mystore.com/{{1}}``).  At send time Meta needs only
    the part that replaces ``{{1}}`` — the *suffix*.

    Strategy:
      1. If the resolved URL shares the same prefix as the template base,
         strip it and return the remainder.
      2. Otherwise (domain mismatch, third-party URL, different scheme),
         return path + query + fragment so the button still works.
    """
    if not full_url:
        return ""

    placeholder = "{{1}}"
    pos = button_url_template.find(placeholder)
    if pos < 0:
        return ""

    base = button_url_template[:pos]

    if full_url.startswith(base):
        return full_url[len(base):]

    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        parsed = urlparse(full_url)
        path_part = parsed.path.lstrip("/")
        query_part = f"?{parsed.query}" if parsed.query else ""
        fragment_part = f"#{parsed.fragment}" if parsed.fragment else ""
        suffix = f"{path_part}{query_part}{fragment_part}"
        logger.info(
            "[AutoEngine] URL domain mismatch — template base=%r vs url=%r; "
            "using path-based suffix=%r",
            base, full_url, suffix,
        )
        return suffix
    except Exception:
        return full_url


def _write_execution(
    db: Session,
    event_id: int,
    automation_id: int,
    customer_id: Optional[int],
    tenant_id: int,
    *,
    status: str,
    skip_reason: Optional[str] = None,
    action_taken: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> int:
    """
    Write an AutomationExecution row and return its PK (after flush).

    ⚠️  GOVERNOR GUARD — خط الدفاع الأخير:
    ────────────────────────────────────────
    يرفض كتابة أي record إذا كان skip_reason ينتمي إلى SOFT_BLOCK_REASONS.

    الـ SOFT BLOCKs (blocked_by_priority, blocked_by_6h_limit,
    blocked_by_daily_limit, blocked_by_cooldown) مؤقتة — كتابة record
    تكسر idempotency check وتمنع إعادة التقييم في الدورة القادمة،
    مما يُضيع الرسالة نهائياً.

    إذا رأيت هذا الـ RuntimeError، فالكود خالف قانون Governor.
    راجع: backend/core/send_governor.py → GOVERNOR DECISION LAW
    """
    if skip_reason is not None:
        try:
            from core.send_governor import SOFT_BLOCK_REASONS as _SOFT  # noqa: PLC0415
            if skip_reason in _SOFT:
                raise RuntimeError(
                    f"[Governor] ILLEGAL: Attempted to write AutomationExecution "
                    f"for soft-block reason '{skip_reason}'. "
                    "Soft blocks MUST return 'delay' without creating execution records — "
                    "otherwise idempotency check will prevent retry and the message is lost. "
                    "Fix: call _write_execution ONLY for hard blocks and successful sends. "
                    "See: backend/core/send_governor.py → GOVERNOR DECISION LAW"
                )
        except ImportError:
            pass  # لو فشل import، استمر (fail-safe)

    from models import AutomationExecution  # noqa: PLC0415

    rec = AutomationExecution(
        tenant_id=tenant_id,
        automation_id=automation_id,
        event_id=event_id,
        customer_id=customer_id,
        status=status,
        skip_reason=skip_reason,
        action_taken=action_taken,
        error_message=error_message,
        executed_at=_utcnow_naive(),
    )
    db.add(rec)
    db.flush()   # populate rec.id so record_sent can reference it
    return rec.id


# ── Interactive (in-window) cart-recovery send ───────────────────────────────

async def _execute_interactive_step(
    db: Session,
    *,
    tenant_id: int,
    event: Any,
    customer: Any,
    wa_conn: Any,
    to_phone: str,
    config: Dict[str, Any],
    active_step: Dict[str, Any],
    automation: Any,
    conversion_decision: Any = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Send a free-form interactive message with dynamic cart-recovery buttons.

    This path is only reached when the customer's service window is open
    (the caller already checked via `has_open_service_window`), so the
    message ships at zero marketing-conversation cost.

    The active step controls everything user-facing:
      • body_text_{ar,en}    — Arabic / English copy with `{{slot}}`
                                placeholders that get expanded against the
                                same slot resolver the template path uses.
      • buttons              — list of action ids (resume_cart,
                                ask_question, postpone, apply_coupon,
                                human_help). Up to 3 are rendered; extras
                                are dropped.
      • cta_labels           — per-action label overrides; defaults to the
                                premium Arabic labels in
                                `services.cart_recovery_buttons`.

    When the active step is the coupon stage (`auto_coupon=True` or
    `message_type=='coupon'`), the message is sent as a Meta CTA-URL
    interactive instead — the primary visual is one big "Use the
    discount now" button that opens the cart with the code attached.
    """
    from services.cart_recovery_buttons import (  # noqa: PLC0415
        ACTION_APPLY_COUPON,
        attach_coupon_to_url,
        build_cta_url_payload,
        build_interactive_payload,
        label_for,
        stage_default_actions,
    )
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    # ── Coupon resolution (mirrors the template path) ────────────────────
    # The conversion layer may have decided up-front that we won't grant
    # a coupon on this send (e.g. cart below the minimum, or the
    # customer already tapped resume_cart). When that's the case we
    # skip the coupon resolver entirely so we don't burn a code and
    # then fail to show it.
    skip_coupon_resolution = bool(
        conversion_decision is not None
        and (
            active_step.get("auto_coupon") is True
            or active_step.get("message_type") == "coupon"
        )
        and not getattr(conversion_decision, "coupon_granted", False)
    )

    if skip_coupon_resolution:
        coupon_extras: Dict[str, str] = {}
        coupon_code = ""
    else:
        _config_with_type = dict(config or {})
        _config_with_type.setdefault(
            "automation_type", getattr(automation, "automation_type", None)
        )
        coupon_extras = await _resolve_auto_coupon(
            db, tenant_id=tenant_id, customer=customer, config=_config_with_type,
            active_step=active_step,
            automation_id=getattr(automation, "id", None),
            event_id=getattr(event, "id", None),
        )
        coupon_code = (
            coupon_extras.get("discount_code")
            or coupon_extras.get("coupon_code")
            or ""
        )

    coupon_percent: Optional[float] = (
        getattr(conversion_decision, "coupon_percent", None)
        if conversion_decision is not None else None
    )

    # ── Resolve every named slot the body might reference ───────────────
    payload = dict(getattr(event, "payload", None) or {})
    cart_url = (
        payload.get("checkout_url")
        or payload.get("cart_url")
        or ""
    )
    cart_id = (
        payload.get("cart_id")
        or payload.get("cart_external_id")
        or payload.get("checkout_id")
        or getattr(event, "id", None)
    )
    customer_name = display_name_passthrough_or_fallback(customer.name)
    store_name = _resolve_store_name(db, tenant_id)
    language = (
        active_step.get("language")
        or config.get("language")
        or "ar"
    ).lower()

    # Body — explicit per-step text wins; otherwise we fall back to the
    # named-slot resolver so a merchant can leave it empty and still get
    # something sensible. The conversion layer gets final say via its
    # body_text_override.
    body_override = getattr(conversion_decision, "body_text_override", None) \
        if conversion_decision is not None else None
    body_template_key = "body_text_en" if language == "en" else "body_text_ar"
    body_template = (
        body_override
        or active_step.get(body_template_key)
        or active_step.get("body_text")
    )
    if not body_template:
        body_template = (
            "{{customer_name}} 🌷\n\n"
            "السلة في {{store_name}} لا تزال محفوظة لك."
        )
    slot_values: Dict[str, str] = {
        "customer_name": customer_name,
        "store_name":    store_name,
        "discount_code": coupon_code,
        "cart_total":    str(payload.get("cart_total") or payload.get("total") or ""),
        "checkout_url":  cart_url,
    }
    body_text = _render_named_slots(body_template, slot_values)

    # Premium coupon presentation — when the conversion layer granted a
    # coupon, splice a copy-friendly block into the body (or append it
    # when the merchant's body didn't include a `{{discount_code}}`
    # placeholder). This is what lets the customer long-press-copy the
    # code on any WhatsApp client while still seeing the primary CTA.
    if coupon_code and (
        conversion_decision is None
        or getattr(conversion_decision, "coupon_granted", False)
    ):
        try:
            from services.conversion_layer import (  # noqa: PLC0415
                enrich_body_with_coupon as _enrich,
            )
            body_text = _enrich(
                body_text, coupon_code, coupon_percent, language=language,
            )
        except Exception:
            pass

    # Stage index — used inside the button id payload so a tap one day
    # later still tells us which stage the customer was on.
    payload_stage = payload.get("step_idx")
    stage = int(payload_stage) if payload_stage is not None else 0

    # Buttons — conversion layer override wins, then merchant pins, then
    # stage defaults. The coupon-granted flag from the layer keeps the
    # dual-CTA contract honest: `apply_coupon` only renders when the
    # layer actually granted a coupon on this send.
    override_buttons = getattr(conversion_decision, "buttons_override", None) \
        if conversion_decision is not None else None
    if override_buttons is not None:
        actions: List[str] = list(override_buttons)
    else:
        actions = list(active_step.get("buttons") or [])
    if not actions:
        actions = stage_default_actions(
            stage,
            with_coupon=bool(coupon_code) or bool(active_step.get("auto_coupon")),
        )

    # Defensive: if apply_coupon slipped through without an actual code,
    # strip it so we never show a CTA the webhook can't honour.
    if not coupon_code and ACTION_APPLY_COUPON in actions:
        actions = [a for a in actions if a != ACTION_APPLY_COUPON]

    cta_labels: Dict[str, str] = dict(active_step.get("cta_labels") or {})
    override_labels = getattr(conversion_decision, "cta_labels_override", None) \
        if conversion_decision is not None else None
    if isinstance(override_labels, dict):
        cta_labels.update(override_labels)

    # ── Dual-CTA vs single-CTA routing ────────────────────────────────
    #
    # Meta interactive messages are mutually-exclusive on button types:
    # either a single CTA-URL button OR up to three reply buttons.
    # Rule:
    #   • apply_coupon ∈ actions  → reply-buttons message with dual CTAs
    #                                (cart + coupon + optional 3rd), and
    #                                the code copy-ready in the body
    #   • coupon stage, no coupon → reply-buttons message with a single
    #                                primary cart CTA (+ escape hatches)
    #   • coupon stage + cart_url → legacy single CTA-URL (only when
    #                                apply_coupon isn't in play; kept for
    #                                merchants who pinned `cta_url` mode)
    automation_id = getattr(automation, "id", None)
    is_coupon_stage = (
        active_step.get("auto_coupon") is True
        or active_step.get("message_type") == "coupon"
        or ACTION_APPLY_COUPON in actions
    )
    prefer_cta_url = (
        is_coupon_stage
        and bool(cart_url)
        and bool(coupon_code)
        and ACTION_APPLY_COUPON not in actions
        and str(active_step.get("coupon_layout") or "").lower() == "cta_url"
    )

    if prefer_cta_url:
        cta_label = (
            cta_labels.get(ACTION_APPLY_COUPON)
            or label_for(ACTION_APPLY_COUPON, language=language)
        )
        send_payload = build_cta_url_payload(
            to_phone   = to_phone,
            body_text  = body_text,
            cta_label  = cta_label,
            cta_url    = attach_coupon_to_url(cart_url, coupon_code or None),
        )
    else:
        send_payload = build_interactive_payload(
            to_phone        = to_phone,
            body_text       = body_text,
            actions         = actions,
            language        = language,
            cart_id         = cart_id,
            coupon_code     = coupon_code or None,
            stage           = stage,
            automation_id   = automation_id,
            cta_labels      = cta_labels,
        )

    try:
        from services.cart_recovery_failures import (  # noqa: PLC0415
            classify_meta_response,
            classify_send_exception,
        )

        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_interactive",
            phone_id=wa_conn.phone_number_id,
            payload=send_payload,
        )

        failure = classify_meta_response(response)
        if failure is not None:
            code, label_ar, raw_meta = failure
            logger.error(
                "[AutoEngine] Interactive send rejected by provider "
                "tenant=%s event=%s code=%s label=%s raw=%s",
                tenant_id, getattr(event, "id", None),
                code, label_ar, raw_meta,
            )
            return False, {
                "error":         code,
                "error_code":    code,
                "error_label":   label_ar,
                "meta_error":    raw_meta,
                "delivery_mode": "interactive",
                "stage":         stage,
                "to":            to_phone,
            }

        try:
            from routers.conversations import record_outbound_message  # noqa: PLC0415
            record_outbound_message(
                db, tenant_id, to_phone, body_text,
                event_type="automation",
                customer_name=customer_name,
                extra={"delivery_mode": "interactive", "stage": stage},
            )
        except Exception:
            pass

        action_info: Dict[str, Any] = {
            "delivery_mode": "interactive",
            "stage":         stage,
            "to":            to_phone,
            "buttons":       actions,
            "coupon_code":   coupon_code or None,
            "coupon_percent": coupon_percent,
            "dual_cta":      (
                ACTION_APPLY_COUPON in actions and "resume_cart" in actions
            ),
            "wa_message_id": (response or {}).get("messages", [{}])[0].get("id"),
            "metrics":       {"sent": 1},
        }
        if conversion_decision is not None:
            action_info["conversion_audit"] = getattr(
                conversion_decision, "audit", {},
            )
        return True, action_info
    except Exception as exc:
        from services.cart_recovery_failures import classify_send_exception  # noqa: PLC0415
        code, label_ar, raw = classify_send_exception(exc)
        logger.error(
            "[AutoEngine] Interactive send failed event=%s automation=%s "
            "tenant=%s code=%s raw=%s",
            getattr(event, "id", None),
            getattr(automation, "id", None),
            tenant_id, code, raw,
        )
        return False, {
            "error":         code,
            "error_code":    code,
            "error_label":   label_ar,
            "exception":     raw,
            "delivery_mode": "interactive",
            "stage":         stage,
            "to":            to_phone,
        }


async def _execute_ai_recovery_step(
    db: Session,
    *,
    tenant_id: int,
    event: Any,
    customer: Any,
    wa_conn: Any,
    to_phone: str,
    config: Dict[str, Any],
    active_step: Dict[str, Any],
    automation_id: Optional[int],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Optional AI-driven recovery turn.

    Sends an AI-generated nudge that references the abandoned cart's
    contents. Kept lightweight — the engine boundary stays Rule-First,
    and we only burn AI tokens when:

      • the merchant has explicitly enabled this stage, AND
      • the customer's service window is open (free-form text allowed).

    When the window has closed we record a `skipped` execution rather
    than falling back to a template — sending a generic template here
    would defeat the point of the AI stage.
    """
    from core.wa_usage import has_open_service_window  # noqa: PLC0415

    if not has_open_service_window(db, tenant_id, to_phone):
        return False, {
            "error":       "ai_recovery_window_closed",
            "error_code":  "ai_recovery_window_closed",
            "error_label": "نافذة الذكاء الاصطناعي مغلقة",
        }

    payload = dict(getattr(event, "payload", None) or {})
    cart_url = (
        payload.get("checkout_url")
        or payload.get("cart_url")
        or ""
    )
    customer_name = display_name_passthrough_or_fallback(customer.name)
    store_name = _resolve_store_name(db, tenant_id)
    cart_total = payload.get("cart_total") or payload.get("total") or ""

    # Try the project's AI client; degrade to a friendly default if the
    # call fails so the engine never blocks on a model outage.
    ai_text: Optional[str] = None
    try:
        from services.ai_client import generate_cart_recovery_text  # noqa: PLC0415
        ai_text = await generate_cart_recovery_text(
            customer_name=customer_name,
            store_name=store_name,
            cart_total=cart_total,
            cart_url=cart_url,
            language=(active_step.get("language") or config.get("language") or "ar"),
            persona=active_step.get("ai_persona") or "concierge",
        )
    except Exception as exc:
        logger.warning(
            "[AutoEngine] AI recovery generation failed tenant=%s: %s",
            tenant_id, exc,
        )

    if not ai_text:
        ai_text = (
            f"مرحباً {customer_name} 🌟\n"
            f"لاحظت سلتك في {store_name} ولم تكتمل بعد. "
            f"إن أردت أساعدك في اختيار البديل المناسب أو الإجابة عن أي سؤال، "
            f"أنا هنا الآن.\n\n{cart_url}".strip()
        )

    from services.cart_recovery_buttons import (  # noqa: PLC0415
        build_interactive_payload,
        stage_default_actions,
    )
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    payload_stage = payload.get("step_idx")
    stage = int(payload_stage) if payload_stage is not None else 0
    actions = list(active_step.get("buttons") or stage_default_actions(stage))

    send_payload = build_interactive_payload(
        to_phone        = to_phone,
        body_text       = ai_text,
        actions         = actions,
        language        = (active_step.get("language") or config.get("language") or "ar"),
        cart_id         = (
            payload.get("cart_id")
            or payload.get("cart_external_id")
            or getattr(event, "id", None)
        ),
        stage           = stage,
        automation_id   = automation_id,
        cta_labels      = active_step.get("cta_labels") or {},
    )

    try:
        from services.cart_recovery_failures import (  # noqa: PLC0415
            classify_meta_response,
            classify_send_exception,
        )

        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_ai_recovery",
            phone_id=wa_conn.phone_number_id,
            payload=send_payload,
        )

        failure = classify_meta_response(response)
        if failure is not None:
            code, label_ar, raw_meta = failure
            logger.error(
                "[AutoEngine] AI recovery send rejected by provider "
                "tenant=%s code=%s label=%s raw=%s",
                tenant_id, code, label_ar, raw_meta,
            )
            return False, {
                "error":         code,
                "error_code":    code,
                "error_label":   label_ar,
                "meta_error":    raw_meta,
                "delivery_mode": "ai_recovery",
                "stage":         stage,
                "to":            to_phone,
            }

        try:
            from routers.conversations import record_outbound_message  # noqa: PLC0415
            record_outbound_message(
                db, tenant_id, to_phone, ai_text,
                event_type="automation",
                customer_name=customer_name,
                extra={"delivery_mode": "ai_recovery", "stage": stage},
            )
        except Exception:
            pass

        return True, {
            "delivery_mode": "ai_recovery",
            "stage":         stage,
            "to":            to_phone,
            "wa_message_id": (response or {}).get("messages", [{}])[0].get("id"),
        }
    except Exception as exc:
        from services.cart_recovery_failures import classify_send_exception  # noqa: PLC0415
        code, label_ar, raw = classify_send_exception(exc)
        logger.error(
            "[AutoEngine] AI recovery send failed tenant=%s code=%s raw=%s",
            tenant_id, code, raw,
        )
        return False, {
            "error":         code,
            "error_code":    code,
            "error_label":   label_ar,
            "exception":     raw,
            "delivery_mode": "ai_recovery",
            "stage":         stage,
            "to":            to_phone,
        }


def _render_named_slots(template: str, values: Dict[str, str]) -> str:
    """
    Tiny named-slot renderer used by the interactive path.

    Handles `{{customer_name}}`-style placeholders. Empty values render
    as empty strings rather than the literal `{{slot}}` text — that's
    the same forgiving behaviour the template path uses for unknown
    Meta variables.
    """
    out = template
    for slot, val in values.items():
        out = out.replace("{{" + slot + "}}", str(val or ""))
    return out


# ── Scheduler loop (called from core/scheduler.py) ───────────────────────────

async def run_automation_engine_scheduler() -> None:
    """
    Background loop — runs process_pending_events for every active tenant
    every POLL_INTERVAL_SECONDS (default 60 s).
    """
    from models import Tenant  # noqa: PLC0415
    from core.database import SessionLocal  # noqa: PLC0415

    # Allow the application to fully start before the first cycle
    await asyncio.sleep(45)
    logger.info(
        "[AutoEngine] Scheduler started — polling every %ds", POLL_INTERVAL_SECONDS
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
                        await process_pending_events(db, tenant.id)
                    except Exception as exc:
                        logger.error(
                            "[AutoEngine] Error processing tenant=%s: %s",
                            tenant.id, exc, exc_info=True,
                        )
            finally:
                db.close()
        except Exception as exc:
            logger.error("[AutoEngine] Scheduler cycle error: %s", exc, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
