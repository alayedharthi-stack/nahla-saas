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


async def process_pending_events(db: Session, tenant_id: int) -> int:
    """
    Scan and process unprocessed AutomationEvent rows for one tenant.
    Returns the total number of WhatsApp messages sent in this cycle.
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

    # ── Master autopilot switch ──────────────────────────────────────────
    # When the merchant has disabled the master switch every pending event
    # is recorded as `skipped` with reason="autopilot_disabled" so it does
    # not pile up forever, and zero messages are sent. We still write the
    # AutomationExecution rows so the dashboard's daily summary reflects
    # the truth (0 sends), and so re-enabling the toggle later does not
    # double-fire on the backlog.
    if not _is_autopilot_enabled(db, tenant_id):
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
            _log_event(
                _EVENTS.AUTOMATION_UNMATCHED_TRIGGER,
                level=logging.WARNING,
                tenant_id=tenant_id,
                event_id=event.id,
                event_type=event.event_type,
                customer_id=event.customer_id,
                reason="no_smart_automation_row_has_this_trigger_event",
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

    # ── Delay check ───────────────────────────────────────────────────────────
    config: Dict[str, Any] = automation.config or {}
    delay_minutes: int = _resolve_delay(config)
    event_age_minutes = (now - _naive_utc(event.created_at)).total_seconds() / 60.0
    if event_age_minutes < delay_minutes:
        remaining = delay_minutes - event_age_minutes
        logger.debug(
            "[AutoEngine] Delay not elapsed event=%s automation=%s remaining=%.1f min",
            event.id, automation.id, remaining,
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

    # ── Execute action ────────────────────────────────────────────────────────
    success, action_info = await _execute_action(db, tenant_id, event, automation, config)
    status = "sent" if success else "failed"

    _write_execution(
        db, event.id, automation.id, event.customer_id, tenant_id,
        status=status,
        action_taken=action_info if success else None,
        error_message=None if success else action_info.get("error"),
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

def _resolve_delay(config: Dict[str, Any]) -> int:
    """Extract delay_minutes from automation config (flat or steps-based)."""
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
    Send a WhatsApp template message to the event's customer.
    Returns (success, info_dict).
    """
    from models import Customer, WhatsAppConnection, WhatsAppTemplate  # noqa: PLC0415
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    customer_id = event.customer_id
    if not customer_id:
        return False, {"error": "no_customer_id"}

    # ── Customer + phone ──────────────────────────────────────────────────────
    customer: Optional[Any] = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .first()
    )
    if not customer or not customer.phone:
        return False, {"error": "no_customer_phone"}

    to_phone = normalize_phone(customer.phone) or customer.phone

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
        return False, {"error": "no_whatsapp_connection"}

    # ── Template lookup ───────────────────────────────────────────────────────
    template: Optional[Any] = None
    if automation.template_id:
        template = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.id == automation.template_id,
                WhatsAppTemplate.status == "APPROVED",
            )
            .first()
        )
    if not template:
        tpl_name = config.get("template_name")
        if tpl_name:
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
        return False, {"error": "no_approved_template"}

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
        active_step=_active_step_for_event(event, config),
    )

    # ── Build template variables ──────────────────────────────────────────────
    vars_map = _build_template_vars(
        event, customer, config,
        template_name=template.name,
        store_name=_resolve_store_name(db, tenant_id),
        coupon_extras=coupon_extras,
    )

    # Build Meta API body components
    body_params = [{"type": "text", "text": str(v)} for v in vars_map.values()]
    components: List[Dict[str, Any]] = []
    if body_params:
        components.append({"type": "body", "parameters": body_params})

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

    # ── Send ──────────────────────────────────────────────────────────────────
    try:
        from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

        response, _ctx = await provider_send_message(
            db,
            wa_conn,
            tenant_id=tenant_id,
            operation="send_template",
            phone_id=wa_conn.phone_number_id,
            payload=send_payload,
        )
        action_info = {
            "template": template.name,
            "to": to_phone,
            "vars": vars_map,
            "wa_message_id": (response or {}).get("messages", [{}])[0].get("id"),
        }
        return True, action_info

    except Exception as exc:
        logger.error(
            "[AutoEngine] Send failed event=%s automation=%s tenant=%s: %s",
            event.id, automation.id, tenant_id, exc,
        )
        return False, {"error": str(exc)[:500]}


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
    customer_name: str = getattr(customer, "name", None) or "العميل"
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
    # explicit var_map. Keeps backwards-compat with merchant-authored
    # templates whose body is just `Hi {{1}}, here: {{2}}`.
    return {
        "{{1}}": customer_name,
        "{{2}}": str(
            payload.get("checkout_url")
            or coupon_extras.get("discount_code")
            or coupon_extras.get("vip_coupon")
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


def _active_step_for_event(event: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    For multi-step automations (e.g. cart_abandoned with 3 reminders), pick
    the step whose `delay_minutes` matches how old the event is. Returns the
    flat config when there are no steps.
    """
    steps = config.get("steps")
    if not isinstance(steps, list) or not steps:
        return {}
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


async def _resolve_auto_coupon(
    db: Session,
    *,
    tenant_id: int,
    customer: Any,
    config: Dict[str, Any],
    active_step: Dict[str, Any],
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
    """
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
) -> None:
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
