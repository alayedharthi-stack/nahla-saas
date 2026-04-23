"""
services/cart_recovery_status.py
────────────────────────────────
Read-only helpers that summarise the **state of the abandoned-cart recovery
sequence** for a given Order, suitable for surfacing in the merchant
dashboard's autopilot queue.

Why a dedicated helper
──────────────────────
Recovery progress is spread across three tables and the JSON payload of
the events themselves:

  • ``Order.extra_metadata.recovery_event_id``  → root cart_abandoned event
  • ``AutomationEvent``                         → root + follow-up stage events
       - ``payload.step_idx``                   → which stage this event represents
       - ``payload.recovery_followups[]``       → emit-time bookkeeping on the root
       - ``payload.recovery_converted_at``      → cancel-on-purchase stamp
       - ``payload.recovery_cancel_reason``     → why the flow was stopped
       - ``processed`` (False)                  → not yet picked up by the engine
       - ``created_at``                         → effective "due" time (may be in
                                                    the future for reschedules)
  • ``AutomationExecution`` (one per (event, automation))
       - ``status``                             → "sent" | "skipped" | "failed"
       - ``executed_at``                        → wall-clock send time
       - ``error_message``                      → only on real send errors
       - ``action_taken.metrics``               → cancel service writes conversion
                                                    metrics here (NOT into status)

Reconstructing the answer to the merchant's basic questions —
"who got reminder #1?", "whose reminder is pending right now?", "whose
reminder failed and why?", "who converted and stopped getting nagged?" —
requires joining all four sources. Doing it inline in the queue handler
would add ~50 lines of fragile JSON-key gymnastics every time we touch
the dashboard, so it lives here behind a stable contract.

Public API
──────────
  • ``summarise_for_orders(db, tenant_id, orders) -> Dict[order_id, RecoverySummary]``
        Compact per-cart summary suitable for the queue list payload.

  • ``timeline_for_order(db, tenant_id, order) -> RecoveryTimeline``
        Full per-cart timeline suitable for a detail drawer / dedicated
        endpoint.

Both are safe to call when the cart has no recovery event yet — they
return ``status="no_recovery"`` so the UI can show a clear empty state
instead of crashing.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from models import AutomationEvent, AutomationExecution, Order

logger = logging.getLogger("nahla.cart_recovery_status")


# ── Status taxonomy ──────────────────────────────────────────────────────────
# Surface status values for the dashboard. These are *derived* — they do
# NOT map 1:1 to AutomationExecution.status which is row-level only.
RECOVERY_STATUS_NO_RECOVERY = "no_recovery"   # cart has no event linked yet
RECOVERY_STATUS_PENDING     = "pending"       # event exists, no execution yet
RECOVERY_STATUS_IN_PROGRESS = "in_progress"   # at least one stage sent, more queued
RECOVERY_STATUS_COMPLETED   = "completed"     # all defined stages sent, no convert
RECOVERY_STATUS_CONVERTED   = "converted"     # customer purchased — flow stopped
RECOVERY_STATUS_FAILED      = "failed"        # latest execution was a real failure


def _isoformat_utc(value: Optional[datetime]) -> Optional[str]:
    """Always emit an offset-aware ISO string so the dashboard can apply
    its Riyadh timezone. The DB stores naive UTC (``datetime.utcnow``),
    so we tag missing tzinfo as UTC rather than letting JSON consumers
    guess."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _resolve_recovery_event_id(order: Order) -> Optional[int]:
    meta = order.extra_metadata or {}
    raw = meta.get("recovery_event_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _step_idx(event: AutomationEvent) -> int:
    """Stage number (1-based) of this event in the recovery sequence.

    The payload stores ``step_idx`` as a 0-based index into the
    automation's ``config.steps`` list:
      step_idx=0 → Stage 1, step_idx=1 → Stage 2, etc.
    Original events (no ``step_idx`` key) are always Stage 1.
    """
    payload = event.payload or {}
    raw = payload.get("step_idx")
    if raw is None:
        return 1
    try:
        return int(raw) + 1
    except (TypeError, ValueError):
        return 1


def _is_purchase_cancel(payload: Dict[str, Any]) -> bool:
    """The cancel service stamps ``recovery_cancel_reason`` whenever the
    flow stops because the customer paid. We treat any of the documented
    reasons (``customer_purchased``, ``order_paid``, etc.) as the same
    "converted" terminal state — the merchant doesn't care about the
    internal taxonomy."""
    return bool(payload.get("recovery_converted_at")) or bool(
        payload.get("recovery_cancel_reason")
    )


def _execution_action_metrics(execution: Optional[AutomationExecution]) -> Dict[str, Any]:
    if execution is None:
        return {}
    action = execution.action_taken or {}
    metrics = action.get("metrics") if isinstance(action, dict) else None
    return metrics if isinstance(metrics, dict) else {}


# ── Internal: build per-event step rows ──────────────────────────────────────
def _step_for_event(
    event: AutomationEvent,
    execution: Optional[AutomationExecution],
    *,
    is_root: bool,
) -> Dict[str, Any]:
    """Map (event, optional execution) → flat dict consumed by the UI."""
    payload = event.payload or {}
    metrics = _execution_action_metrics(execution)

    if execution is not None:
        # The engine wrote a row, so this stage has been **attempted**.
        if execution.status == "sent":
            stage_status = "sent"
        elif execution.status == "skipped":
            stage_status = "skipped"
        elif execution.status == "failed":
            # Cancel service marks failure with metrics.skip_reason set
            # but no error_message. Distinguish "real failure" from
            # "skipped because converted" so the UI can colour correctly.
            if metrics.get("skip_reason") and not (execution.error_message or ""):
                stage_status = "skipped"
            else:
                stage_status = "failed"
        else:
            stage_status = execution.status or "unknown"
    else:
        # No execution row = engine has not picked it up yet.
        # Either the event is in the future (reschedule) or it's due now
        # but the next 60s tick hasn't fired yet.
        stage_status = "pending"

    action = execution.action_taken if execution and isinstance(execution.action_taken, dict) else {}

    # Pull the structured failure code + Arabic label out of the action
    # payload first, then fall back to the legacy text in
    # ``error_message`` (which may itself be a structured code now).
    # Carrying both lets the UI:
    #   - render the localised ``failure_label`` directly (no English token), and
    #   - branch on ``failure_code`` for icons / retry eligibility.
    failure_code: Optional[str] = None
    failure_label: Optional[str] = None
    meta_error: Optional[Dict[str, Any]] = None
    if isinstance(action, dict):
        failure_code  = action.get("error_code") or action.get("error")
        failure_label = action.get("error_label")
        raw_meta = action.get("meta_error")
        if isinstance(raw_meta, dict):
            meta_error = raw_meta

    if stage_status == "failed" and execution is not None and not failure_code:
        # Legacy rows written before the structured payload existed —
        # try to classify the bare ``error_message`` string.
        try:
            from services.cart_recovery_failures import classify_internal_error  # noqa: PLC0415
            code, label = classify_internal_error(execution.error_message or "")
            failure_code = code
            failure_label = label
        except Exception:
            failure_label = execution.error_message or None

    return {
        "step_idx":      _step_idx(event),
        "event_id":      event.id,
        "is_root":       is_root,
        "status":        stage_status,
        "scheduled_at":  _isoformat_utc(event.created_at),
        "sent_at":       _isoformat_utc(execution.executed_at) if execution else None,
        "error":         (execution.error_message or None) if execution else None,
        # ``failure_code`` / ``failure_label`` are the post-fix dashboard
        # contract — taxonomised, Arabic, and safe to render directly.
        "failure_code":  failure_code if stage_status == "failed" else None,
        "failure_label": failure_label if stage_status == "failed" else None,
        "meta_error":    meta_error if stage_status == "failed" else None,
        "skip_reason":   metrics.get("skip_reason") if metrics else (
            payload.get("recovery_cancel_reason") if stage_status == "skipped" else None
        ),
        "wa_message_id": action.get("wa_message_id") if isinstance(action, dict) else None,
        "channel":       action.get("channel") or action.get("delivery_mode") if isinstance(action, dict) else None,
        "template_name": action.get("template_name") or action.get("template") if isinstance(action, dict) else None,
    }


# ── Internal: load the full event tree for a root id ────────────────────────
def _load_event_tree(
    db: Session,
    tenant_id: int,
    root_event_id: int,
) -> List[AutomationEvent]:
    """Return the root + every direct follow-up event in DB-id order.

    Follow-ups carry ``payload.parent_event_id == root_event_id`` (set by
    ``_reschedule_followup_event`` and the sweeper). Two query layers:

      1. **JSONB indexed predicate** on PostgreSQL — the production path,
         O(log n) thanks to the GIN index on ``automation_events.payload``.
      2. **Python-side scan** — used when (a) the dialect doesn't expose
         ``.astext`` (SQLite / older PG) or (b) the indexed query returns
         nothing yet still has follow-ups in the table (older rows
         written before the predicate column was added). The scan is
         bounded to ``cart_abandoned`` events for this tenant only, so
         even a noisy production table stays sub-second.

    Always doing the second pass when the first returns empty lets the
    test suite (SQLite) and any merchant whose first follow-up didn't
    persist a clean ``parent_event_id`` integer literal still see the
    right tree.
    """
    rows: List[AutomationEvent] = []

    root = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.id == root_event_id,
        )
        .first()
    )
    if root is None:
        return rows
    rows.append(root)

    followups: List[AutomationEvent] = []
    try:
        followups = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.payload["parent_event_id"].astext == str(root_event_id),
            )
            .order_by(AutomationEvent.id.asc())
            .all()
        )
    except Exception as exc:
        logger.debug(
            "[recovery_status] JSONB followup query failed for root=%s tenant=%s: %s",
            root_event_id, tenant_id, exc,
        )
        followups = []

    if not followups:
        # Python-side fallback: scan ``cart_abandoned`` for this tenant
        # and match ``parent_event_id`` regardless of whether it was
        # stored as an int or a string. This also catches the case where
        # JSON type cannot use the .astext predicate (SQLite tests).
        candidates = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.event_type == "cart_abandoned",
                AutomationEvent.id != root_event_id,
            )
            .order_by(AutomationEvent.id.asc())
            .all()
        )
        for cand in candidates:
            payload = cand.payload or {}
            if str(payload.get("parent_event_id")) == str(root_event_id):
                followups.append(cand)

    rows.extend(followups)

    # Second level: follow-ups of retry events (grandchildren of root).
    # When a manual retry creates event R with parent=root, and the
    # sweeper later emits follow-ups F2/F3 with parent=R, those are
    # still part of this cart's timeline.
    retry_ids = [f.id for f in followups if f.id != root_event_id]
    if retry_ids:
        grandchildren: List[AutomationEvent] = []
        seen = {r.id for r in rows}
        try:
            for rid in retry_ids:
                gcs = (
                    db.query(AutomationEvent)
                    .filter(
                        AutomationEvent.tenant_id == tenant_id,
                        AutomationEvent.payload["parent_event_id"].astext == str(rid),
                    )
                    .order_by(AutomationEvent.id.asc())
                    .all()
                )
                grandchildren.extend(gcs)
        except Exception:
            for cand in (
                db.query(AutomationEvent)
                .filter(
                    AutomationEvent.tenant_id == tenant_id,
                    AutomationEvent.event_type == "cart_abandoned",
                )
                .all()
            ):
                p = (cand.payload or {}).get("parent_event_id")
                if p is not None and int(p) in {int(r) for r in retry_ids}:
                    grandchildren.append(cand)
        for gc in grandchildren:
            if gc.id not in seen:
                rows.append(gc)
                seen.add(gc.id)

    return rows


def _load_executions_for_events(
    db: Session,
    tenant_id: int,
    event_ids: Sequence[int],
) -> Dict[int, AutomationExecution]:
    """One execution per ``(event, automation)``. We collapse to the
    first non-``skipped`` one if multiple exist, falling back to the
    most-recently-written row otherwise — the merchant cares about
    "did this stage actually get sent" more than which automation
    object emitted it."""
    if not event_ids:
        return {}

    rows = (
        db.query(AutomationExecution)
        .filter(
            AutomationExecution.tenant_id == tenant_id,
            AutomationExecution.event_id.in_(list(event_ids)),
        )
        .order_by(AutomationExecution.executed_at.desc())
        .all()
    )

    by_event: Dict[int, AutomationExecution] = {}
    for row in rows:
        existing = by_event.get(row.event_id)
        if existing is None:
            by_event[row.event_id] = row
            continue
        # Prefer "sent" over "skipped" / "failed" so the dashboard reflects
        # the actual delivery rather than an earlier short-circuit.
        if existing.status != "sent" and row.status == "sent":
            by_event[row.event_id] = row

    return by_event


# ── Public API: per-cart summary ─────────────────────────────────────────────
def _get_configured_steps(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Return the stage configs from the tenant's abandoned-cart automation.

    Falls back to a 3-stage default when no automation is found.
    """
    from models import SmartAutomation  # noqa: PLC0415

    auto = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type == "abandoned_cart",
        )
        .first()
    )
    if auto and auto.config:
        steps = auto.config.get("steps") or []
        if steps:
            return list(steps)
    return [
        {"delay_minutes": 30, "enabled": True},
        {"delay_minutes": 360, "enabled": True},
        {"delay_minutes": 1425, "enabled": True},
    ]


def _get_total_configured_stages(db: Session, tenant_id: int) -> int:
    return len(_get_configured_steps(db, tenant_id))


def summarise_for_orders(
    db: Session,
    tenant_id: int,
    orders: Iterable[Order],
) -> Dict[int, Dict[str, Any]]:
    """Return ``{order_id: summary_dict}``."""
    out: Dict[int, Dict[str, Any]] = {}

    roots_by_order: Dict[int, int] = {}
    for order in orders:
        rid = _resolve_recovery_event_id(order)
        if rid is not None:
            roots_by_order[order.id] = rid
        out[order.id] = _empty_summary()

    if not roots_by_order:
        return out

    total_stages = _get_total_configured_stages(db, tenant_id)

    for order_id, root_id in list(roots_by_order.items())[:200]:
        events = _load_event_tree(db, tenant_id, root_id)
        if not events:
            out[order_id] = _empty_summary()
            continue

        executions = _load_executions_for_events(
            db, tenant_id, [e.id for e in events]
        )
        out[order_id] = _summary_from_tree(
            events, executions, total_configured_stages=total_stages,
        )

    return out


def _empty_summary() -> Dict[str, Any]:
    return {
        "status":              RECOVERY_STATUS_NO_RECOVERY,
        "steps_sent":          0,
        "steps_failed":        0,
        "total_stages":        0,
        "last_sent_at":        None,
        "last_status":         None,
        "last_error":          None,
        "last_failure_code":   None,
        "last_failure_label":  None,
        "next_pending_at":     None,
        "converted_at":        None,
        "cancel_reason":       None,
        "recovery_event_id":   None,
    }


def _summary_from_tree(
    events: List[AutomationEvent],
    executions: Dict[int, AutomationExecution],
    *,
    total_configured_stages: int = 3,
) -> Dict[str, Any]:
    root = events[0]
    root_payload = root.payload or {}

    steps = [
        _step_for_event(ev, executions.get(ev.id), is_root=(ev.id == root.id))
        for ev in events
    ]

    sent = [s for s in steps if s["status"] == "sent"]
    failed = [s for s in steps if s["status"] == "failed"]
    pending = [s for s in steps if s["status"] == "pending"]

    last_sent = max(sent, key=lambda s: s.get("sent_at") or "", default=None)
    last_step = max(steps, key=lambda s: (s.get("sent_at") or s.get("scheduled_at") or ""))
    next_pending = min(pending, key=lambda s: s.get("scheduled_at") or "", default=None)

    converted_at = root_payload.get("recovery_converted_at")
    cancel_reason = root_payload.get("recovery_cancel_reason")

    max_sent_stage = max((s["step_idx"] for s in sent), default=0) if sent else 0

    if converted_at or cancel_reason:
        status = RECOVERY_STATUS_CONVERTED
    elif failed and not sent:
        status = RECOVERY_STATUS_FAILED
    elif pending and not sent:
        status = RECOVERY_STATUS_PENDING
    elif sent and pending:
        status = RECOVERY_STATUS_IN_PROGRESS
    elif sent and not pending:
        # Only mark completed if the highest sent stage covers all
        # configured stages.  When the follow-up sweeper hasn't run
        # yet (only stage 1 sent, stages 2-3 not emitted), there are
        # zero pending events — but the sequence is NOT done.
        if max_sent_stage >= total_configured_stages:
            status = RECOVERY_STATUS_COMPLETED
        else:
            status = RECOVERY_STATUS_IN_PROGRESS
    else:
        status = RECOVERY_STATUS_PENDING

    last_failed = max(failed, key=lambda s: s.get("sent_at") or s.get("scheduled_at") or "", default=None)

    return {
        "status":              status,
        "steps_sent":          len(sent),
        "steps_failed":        len(failed),
        "total_stages":        total_configured_stages,
        "last_sent_at":        last_sent.get("sent_at") if last_sent else None,
        "last_status":         last_step.get("status") if last_step else None,
        "last_error":          last_step.get("error") if last_step else None,
        "last_failure_code":   last_failed.get("failure_code") if last_failed else None,
        "last_failure_label":  last_failed.get("failure_label") if last_failed else None,
        "next_pending_at":     next_pending.get("scheduled_at") if next_pending else None,
        "converted_at":        converted_at,
        "cancel_reason":       cancel_reason,
        "recovery_event_id":   root.id,
    }


# ── Public API: full per-cart timeline ───────────────────────────────────────
_STAGE_LABELS = {
    1: "تذكير أول",
    2: "متابعة",
    3: "تذكير أخير",
    4: "عرض خاص",
}


def _format_delay(minutes: int) -> str:
    if minutes < 60:
        return f"بعد {minutes} دقيقة"
    hours = minutes / 60
    if hours == int(hours):
        h = int(hours)
        if h == 1:
            return "بعد ساعة"
        if h == 2:
            return "بعد ساعتين"
        if h <= 10:
            return f"بعد {h} ساعات"
        return f"بعد {h} ساعة"
    h = int(hours)
    m = minutes - h * 60
    return f"بعد {h} ساعة و {m} دقيقة"


def timeline_for_order(
    db: Session,
    tenant_id: int,
    order: Order,
) -> Dict[str, Any]:
    """Return the full step-by-step recovery timeline for one order.

    Includes all configured stages — even those not yet emitted as
    events — so the merchant sees the complete recovery plan.
    """
    root_id = _resolve_recovery_event_id(order)
    if root_id is None:
        return {**_empty_summary(), "steps": []}

    events = _load_event_tree(db, tenant_id, root_id)
    if not events:
        return {**_empty_summary(), "steps": []}

    configured_steps = _get_configured_steps(db, tenant_id)
    total_stages = len(configured_steps)
    executions = _load_executions_for_events(db, tenant_id, [e.id for e in events])
    summary = _summary_from_tree(
        events, executions, total_configured_stages=total_stages,
    )

    real_steps = [
        _step_for_event(ev, executions.get(ev.id), is_root=(ev.id == events[0].id))
        for ev in events
    ]
    real_steps.sort(key=lambda s: (s["step_idx"], s["event_id"]))

    existing_idxs = {s["step_idx"] for s in real_steps}

    root_created = events[0].created_at
    if root_created and root_created.tzinfo is None:
        root_created = root_created.replace(tzinfo=timezone.utc)

    from datetime import timedelta  # noqa: PLC0415
    for i, cfg in enumerate(configured_steps):
        stage_num = i + 1
        if stage_num in existing_idxs:
            continue
        if not cfg.get("enabled", True):
            continue
        delay = int(cfg.get("delay_minutes") or 0)
        scheduled = None
        if root_created:
            scheduled = root_created + timedelta(minutes=delay)

        real_steps.append({
            "step_idx":      stage_num,
            "event_id":      0,
            "is_root":       False,
            "status":        "upcoming",
            "scheduled_at":  _isoformat_utc(scheduled),
            "sent_at":       None,
            "error":         None,
            "failure_code":  None,
            "failure_label": None,
            "meta_error":    None,
            "skip_reason":   None,
            "wa_message_id": None,
            "channel":       None,
            "template_name": None,
            "label":         _STAGE_LABELS.get(stage_num, f"المرحلة {stage_num}"),
            "delay_minutes": delay,
            "delay_label":   _format_delay(delay),
        })

    real_steps.sort(key=lambda s: (s["step_idx"], s.get("event_id") or 0))

    for s in real_steps:
        idx = s["step_idx"]
        if "label" not in s:
            s["label"] = _STAGE_LABELS.get(idx, f"المرحلة {idx}")
        if "delay_minutes" not in s and idx - 1 < len(configured_steps):
            cfg = configured_steps[idx - 1]
            s["delay_minutes"] = int(cfg.get("delay_minutes") or 0)
            s["delay_label"] = _format_delay(s["delay_minutes"])

    return {**summary, "steps": real_steps}
