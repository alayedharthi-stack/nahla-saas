"""One-shot, scoped, read-only diagnostic; never starts the application.

Question 1 (tenant 1): two cash-on-delivery orders were set to cancelled a day
or more after the order recorded a cash-on-delivery confirmation. Which path
cancelled them? The repository has four paths that can (see the evaluation
notes): the scheduled COD reminder / auto-cancel sweep
(core.automation_emitters.scan_cod_confirmations: status cancelled, metadata
cod_auto_cancelled_at / cod_auto_cancel_reason, system event
order.cod.auto_cancelled, no store call); a customer cancel over WhatsApp
(services.cod_confirmation.handle_cod_reply: store call first, then
cod_cancelled_at and system event order.cod.cancelled); a store-side status
synced in (webhook or poller: status overwritten with the store's slug, no
cancel metadata); and the dashboard cancel (WhatsApp-origin orders only).

For every tenant-1 order that carries a COD confirmation and is now cancelled:
an alias (never the id, number, total, name or phone), the COD timeline from
its metadata (timestamps and reasons only), its system events, the store
webhooks that name it (event type, time, processing status, the status slug
they carried), its lifecycle ledger rows, and whether any message in its
conversation window mentions cancelling (a flag, never the text). Plus the
tenant's cod_confirmation automation: enabled and its timing settings.

Platform-wide, counts only: orders with a COD confirmation that the sweep
later auto-cancelled, and tenants with the automation enabled.

Question 2: which model the production commerce runtime turns are served by
(ai_usage_events, reason commerce_runtime_pilot, last 14 days): model name and
counts only.

Session: default_transaction_read_only=on, statement_timeout 15s, refuses any
host other than the production database it names. Writes are impossible.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

CONFIRMATION = "READ_ONLY_COD_CANCEL_TRACE_V1"
TENANT = 1
DIGITS = re.compile(r"\d{4,}")
CANCEL_WORDS = ("إلغاء", "الغاء", "الغي", "ألغي", "cancel")
COD_KEYS = ("created_at", "nahla_cod_confirmation_sent", "nahla_cod_confirmation_sent_at",
            "cod_confirm_requested_at", "cod_confirmed_at", "cod_previous_status",
            "cod_auto_cancelled_at", "cod_auto_cancel_reason", "cod_cancelled_at",
            "cod_cancel_store_update_failed_at", "cancelled_at", "cancel_reason", "payment_method",
            "lifecycle")
TIMING_KEYS = ("cancel_after_minutes", "steps", "reminder_minutes", "delays", "delay_minutes")


def emit(kind, value):
    print("READONLY_TRACE=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str),
          flush=True)


def mask(value, limit=200):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def _small(value):
    """A metadata value safe to print: timestamps, slugs, flags, small numbers."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if abs(value) < 100000 else "[number]"
    if isinstance(value, (list, tuple)):
        return f"[list:{len(value)}]"
    if isinstance(value, dict):
        return {str(k): _small(v) for k, v in list(value.items())[:8]}
    text_value = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ][\d:.+\-Z]*", text_value):
        return text_value[:40]
    return mask(text_value, 60)


def automation(conn):
    rows = conn.execute(text(
        "SELECT enabled, config, created_at, updated_at FROM smart_automations "
        "WHERE tenant_id = :t AND automation_type = 'cod_confirmation' ORDER BY id"), {"t": TENANT}).all()
    for enabled, config, created_at, updated_at in rows:
        config = config or {}
        emit("cod_automation", {"enabled": enabled, "created_at": created_at, "updated_at": updated_at,
                                "timing": {k: _small(config.get(k)) for k in TIMING_KEYS if k in config},
                                "config_keys": sorted(config)[:20]})
    if not rows:
        emit("cod_automation", {"rows": 0})


def orders(conn):
    rows = conn.execute(text(
        "SELECT id, external_id, status, source, customer_id, customer_info, metadata FROM orders "
        "WHERE tenant_id = :t AND metadata ? 'cod_confirmed_at' "
        "AND lower(status) IN ('cancelled', 'canceled') ORDER BY id"), {"t": TENANT}).all()
    emit("orders_found", {"count": len(rows)})
    for index, (order_id, external_id, status, source, customer_id, info, meta) in enumerate(rows, 1):
        alias = f"order_{chr(64 + index)}"
        meta = meta or {}
        phone = str((info or {}).get("phone") or (info or {}).get("mobile") or "")
        emit("order", {"alias": alias, "status": status, "source": source,
                       "linked_by": "customer_id" if customer_id else "phone_only",
                       "cod": {k: _small(meta.get(k)) for k in COD_KEYS if k in meta},
                       "cod_reminders": _small(meta.get("cod_reminders"))})
        events = conn.execute(text(
            "SELECT event_type, severity, created_at, payload FROM system_events "
            "WHERE tenant_id = :t AND reference_id = :r ORDER BY created_at"),
            {"t": TENANT, "r": str(order_id)}).all()
        for event_type, severity, created_at, payload in events:
            payload = payload or {}
            emit("system_event", {"alias": alias, "event_type": event_type, "severity": severity,
                                  "created_at": created_at,
                                  "payload": {k: _small(payload.get(k)) for k in
                                              ("elapsed_minutes", "cancel_after_minutes", "status",
                                               "reason", "step") if k in payload}})
        if external_id:
            hooks = conn.execute(text(
                "SELECT event_type, received_at, status, parsed_payload FROM webhook_events "
                "WHERE provider = 'salla' AND (tenant_id = :t OR tenant_id IS NULL) "
                "AND received_at > now() - interval '21 days' "
                "AND parsed_payload::text LIKE :x ORDER BY received_at"),
                {"t": TENANT, "x": f"%{external_id}%"}).all()
            for event_type, received_at, hook_status, parsed in hooks:
                data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
                slug = None
                if isinstance(data, dict):
                    raw = data.get("status")
                    slug = (raw.get("slug") or raw.get("name")) if isinstance(raw, dict) else raw
                emit("store_webhook", {"alias": alias, "event_type": event_type,
                                       "received_at": received_at, "processing": hook_status,
                                       "status_slug": mask(slug, 40)})
        ledger = conn.execute(text(
            "SELECT business_intent, channel, created_at FROM commerce_lifecycle_notification_ledger "
            "WHERE tenant_id = :t AND order_id = :o ORDER BY created_at"),
            {"t": TENANT, "o": int(order_id)}).all()
        for intent, channel, created_at in ledger:
            emit("lifecycle_ledger", {"alias": alias, "business_intent": intent, "channel": channel,
                                      "created_at": created_at})
        if phone:
            digits = re.sub(r"\D", "", phone)[-9:]
            messages = conn.execute(text(
                "SELECT direction, event_type, created_at, body FROM message_events "
                "WHERE tenant_id = :t AND created_at > now() - interval '21 days' "
                "AND (metadata->>'customer_phone' LIKE :p OR metadata->>'phone' LIKE :p) "
                "ORDER BY created_at"), {"t": TENANT, "p": f"%{digits}"}).all()
            cancelling = [(d, e, c) for d, e, c, b in messages
                          if any(w in str(b or "").lower() for w in CANCEL_WORDS)]
            emit("messages", {"alias": alias, "in_window": len(messages),
                              "mentioning_cancel": [{"direction": d, "event_type": e, "created_at": c}
                                                    for d, e, c in cancelling][:12]})


def platform(conn):
    confirmed_then_swept = conn.execute(text(
        "SELECT count(DISTINCT o.id), count(DISTINCT o.tenant_id) FROM orders o "
        "JOIN system_events e ON e.tenant_id = o.tenant_id AND e.reference_id = o.id::text "
        "AND e.event_type = 'order.cod.auto_cancelled' "
        "WHERE o.metadata ? 'cod_confirmed_at' "
        "AND e.created_at > (o.metadata->>'cod_confirmed_at')::timestamptz")).one()
    sweeps = conn.execute(text(
        "SELECT count(*) FROM system_events WHERE event_type = 'order.cod.auto_cancelled' "
        "AND created_at > now() - interval '30 days'")).scalar()
    enabled = conn.execute(text(
        "SELECT count(DISTINCT tenant_id) FROM smart_automations "
        "WHERE automation_type = 'cod_confirmation' AND enabled")).scalar()
    emit("platform", {"confirmed_orders_auto_cancelled_after_confirmation": confirmed_then_swept[0],
                      "tenants_affected": confirmed_then_swept[1],
                      "auto_cancel_events_last_30d": sweeps,
                      "tenants_with_cod_automation_enabled": enabled})


def runtime_model(conn):
    rows = conn.execute(text(
        "SELECT model, count(*), min(created_at), max(created_at) FROM ai_usage_events "
        "WHERE reason = 'commerce_runtime_pilot' AND created_at > now() - interval '14 days' "
        "GROUP BY model ORDER BY max(created_at) DESC")).all()
    for model, count, first, last in rows:
        emit("runtime_model", {"model": model, "calls": count, "first": first, "last": last})
    if not rows:
        emit("runtime_model", {"rows": 0})


def main():
    if os.environ.get("NAHLA_HISTORY_TRACE_CONFIRM") != CONFIRMATION:
        emit("status", {"status": "idle"})
        return 0
    try:
        raw = os.environ.get("DATABASE_URL", "")
        parsed = make_url(raw.replace("postgres://", "postgresql://", 1))
        if (parsed.host, parsed.port or 5432, parsed.database) != (
                "postgres-ancu.railway.internal", 5432, "railway"):
            emit("status", {"status": "target_refused"})
            return 1
        engine = create_engine(parsed, connect_args={
            "options": "-c default_transaction_read_only=on -c statement_timeout=15000"})
        with engine.connect() as conn:
            if conn.execute(text("SHOW default_transaction_read_only")).scalar() != "on":
                emit("status", {"status": "not_read_only"})
                return 1
            for part, fn in (("runtime_model", runtime_model), ("cod_automation", automation),
                             ("orders", orders), ("platform", platform)):
                try:
                    fn(conn)
                except Exception as exc:  # noqa: BLE001 - one part failing does not hide the others
                    conn.rollback()
                    emit("part_failed", {"failed": part, "error_type": type(exc).__name__,
                                         "detail": mask(str(exc), 160)})
            emit("status", {"status": "done"})
        return 0
    except Exception as exc:  # raw errors may carry query values; only the type is logged
        emit("status", {"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
