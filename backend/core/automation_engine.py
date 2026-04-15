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
    """
    from models import AutomationEvent  # noqa: PLC0415

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

    logger.debug(
        "[AutoEngine] emit tenant=%s type=%s customer=%s",
        tenant_id, event_type, customer_id,
    )
    return ev


# ── Public: main processing entry point ──────────────────────────────────────

async def process_pending_events(db: Session, tenant_id: int) -> int:
    """
    Scan and process unprocessed AutomationEvent rows for one tenant.
    Returns the total number of WhatsApp messages sent in this cycle.
    """
    from models import AutomationEvent  # noqa: PLC0415

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


# ── Internal: event processing ────────────────────────────────────────────────

async def _process_event(
    db: Session, tenant_id: int, event: Any, now: datetime
) -> int:
    """
    Find matching automations for one event and try to execute each.
    Returns the number of messages actually sent.
    """
    from models import SmartAutomation  # noqa: PLC0415

    automations: List[Any] = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.trigger_event == event.event_type,
            SmartAutomation.enabled.is_(True),
        )
        .all()
    )

    if not automations:
        # No automation is configured for this event type — mark done
        event.processed = True
        logger.debug(
            "[AutoEngine] No matching automation for event=%s type=%s — marking processed",
            event.id, event.event_type,
        )
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
    passed, skip_reason = _evaluate_conditions(db, event, config)
    if not passed:
        _write_execution(
            db, event.id, automation.id, event.customer_id, tenant_id,
            status="skipped", skip_reason=skip_reason,
        )
        logger.info(
            "[AutoEngine] SKIPPED event=%s automation=%s reason=%s tenant=%s",
            event.id, automation.id, skip_reason, tenant_id,
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

    logger.info(
        "[AutoEngine] %s event=%s type=%s automation=%s tenant=%s customer=%s",
        status.upper(), event.id, event.event_type,
        automation.id, tenant_id, event.customer_id,
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

    # ── Build template variables ──────────────────────────────────────────────
    vars_map = _build_template_vars(event, customer, config)

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
    event: Any, customer: Any, config: Dict[str, Any]
) -> Dict[str, str]:
    """
    Build a flat ordered dict of template variable values.
    Priority: explicit var_map from config > event payload > customer defaults.
    """
    customer_name: str = getattr(customer, "name", None) or "العميل"
    payload: Dict[str, Any] = event.payload or {}

    # Use var_map from config if provided (keys like "{{1}}", "{{2}}")
    var_map: Dict[str, str] = config.get("var_map") or {}
    if var_map:
        resolved: Dict[str, str] = {}
        for placeholder, field in var_map.items():
            if field == "customer_name":
                resolved[placeholder] = customer_name
            elif field == "product_name":
                resolved[placeholder] = str(payload.get("product_name", ""))
            elif field == "reorder_url":
                resolved[placeholder] = str(payload.get("reorder_url", ""))
            elif field == "checkout_url":
                resolved[placeholder] = str(payload.get("checkout_url", ""))
            elif field == "coupon_code":
                resolved[placeholder] = str(
                    config.get("coupon_code") or payload.get("coupon_code", "")
                )
            else:
                resolved[placeholder] = str(payload.get(field, ""))
        return resolved

    # Fallback: simple positional defaults
    return {
        "{{1}}": customer_name,
        "{{2}}": str(payload.get("checkout_url") or payload.get("coupon_code") or ""),
    }


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
