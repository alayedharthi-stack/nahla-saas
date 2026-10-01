"""One-shot, scoped, read-only diagnostic; never starts the application.

COD auto-cancel consistency (platform-wide). The scheduled COD sweep
(core.automation_emitters.scan_cod_confirmations) selects orders by status
alone; ``under_review`` - the status a customer's COD confirmation writes - is
in its set, so confirmed orders were cancelled for "no customer response",
locally only. For every order the sweep auto-cancelled, and for every order it
would act on now, this reads:

* an alias (tenant + running index; never the order id, number, external id,
  total, name or phone);
* the order's local status now, source, and payment-method class;
* the trusted COD state: whether Nahla asked for confirmation (send stamp), the
  customer's recorded decision (metadata keys and order.cod.* system events),
  and when, relative to creation and to the first auto-cancel;
* reminders the sweep sent after the customer had confirmed;
* the store's side: the latest store webhook that names the order (event type,
  time, status slug), and whether any store webhook carried a cancelled status;
* how many times the sweep cancelled it, and whether the local status has
  since been restored by sync.

Then a classification for a remediation plan, and the orders the live sweep
would cancel or remind next although the customer confirmed (at risk now).

Session: default_transaction_read_only=on, statement_timeout 15s, refuses any
host other than the production database it names. Writes are impossible.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

CONFIRMATION = "READ_ONLY_COD_CONSISTENCY_V1"
DIGITS = re.compile(r"\d{4,}")
COD_METHODS = {"cod", "cash_on_delivery", "cod_payment", "cash"}
CONFIRM_KEYS = ("cod_confirmed_at", "cod_pushed_external_id", "cod_confirm_requested_at")
SWEEP_SLUGS = {"pending_confirmation", "awaiting_confirmation", "under_review", "in_review"}
SWEEP_ARABIC = {"بانتظار التأكيد", "بإنتظار التأكيد", "قيد المراجعة", "بانتظار المراجعة",
                "بإنتظار المراجعة", "بانتظار تأكيد العميل"}
CANCELLED = {"cancelled", "canceled", "canceled_by_admin", "cancelled_by_admin"}


def emit(kind, value):
    print("READONLY_TRACE=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str),
          flush=True)


def mask(value, limit=40):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def ts(value):
    if isinstance(value, datetime):
        out = value
    elif value:
        try:
            out = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    if out.tzinfo is not None:
        out = out.astimezone(timezone.utc).replace(tzinfo=None)
    return out


def minutes(a, b):
    return None if a is None or b is None else int((b - a).total_seconds() // 60)


def in_sweep(status):
    raw = str(status or "").strip()
    return raw.lower() in SWEEP_SLUGS or raw in SWEEP_ARABIC


def payment_class(meta):
    method = str(meta.get("payment_method") or "").strip().lower()
    if not method:
        return "missing"
    return "cod" if method in COD_METHODS else "other:" + mask(method, 24)


def events_for(conn, tenant, order_id):
    rows = conn.execute(text(
        "SELECT event_type, count(*), min(created_at), max(created_at) FROM system_events "
        "WHERE tenant_id = :t AND reference_id = :r AND event_type LIKE 'order.cod.%' "
        "GROUP BY event_type"), {"t": tenant, "r": str(order_id)}).all()
    return {event_type: (count, ts(first), ts(last)) for event_type, count, first, last in rows}


def store_side(conn, tenant, external_id):
    if not external_id:
        return {"webhooks": 0}
    rows = conn.execute(text(
        "SELECT event_type, received_at, parsed_payload FROM webhook_events "
        "WHERE provider = 'salla' AND (tenant_id = :t OR tenant_id IS NULL) "
        "AND received_at > now() - interval '45 days' AND parsed_payload::text LIKE :x "
        "ORDER BY received_at"), {"t": tenant, "x": f"%{external_id}%"}).all()
    slugs = []
    for event_type, received_at, parsed in rows:
        data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
        slug = None
        if isinstance(data, dict):
            raw = data.get("status")
            slug = (raw.get("slug") or raw.get("name")) if isinstance(raw, dict) else raw
        slugs.append((event_type, ts(received_at), str(slug or "").strip().lower()))
    if not slugs:
        return {"webhooks": 0}
    last = slugs[-1]
    with_status = [s for s in slugs if s[2]]
    last_status = with_status[-1] if with_status else None
    return {"webhooks": len(slugs), "last_event": last[0], "last_at": last[1],
            "last_status": mask(last_status[2]) if last_status else None,
            "last_status_at": last_status[1] if last_status else None,
            "store_cancelled": any(s[2] in CANCELLED for s in slugs)}


def classify(confirmed, requested, local_status, store):
    store_status = store.get("last_status")
    if store.get("store_cancelled") and store_status in CANCELLED:
        return "store_also_cancelled"
    if confirmed:
        return "confirmed_cancelled_by_sweep" if local_status in CANCELLED else "confirmed_restored_by_sync"
    if requested:
        return "unconfirmed_cod_cancelled_locally" if local_status in CANCELLED else "unconfirmed_restored_by_sync"
    return "never_asked_cancelled_by_sweep" if local_status in CANCELLED else "never_asked_restored_by_sync"


def affected(conn):
    rows = conn.execute(text(
        "SELECT o.id, o.tenant_id, o.external_id, o.status, o.source, o.metadata, "
        "e.cnt, e.first_at, e.last_at FROM orders o JOIN ("
        "  SELECT tenant_id, reference_id, count(*) AS cnt, min(created_at) AS first_at, "
        "  max(created_at) AS last_at FROM system_events "
        "  WHERE event_type = 'order.cod.auto_cancelled' GROUP BY tenant_id, reference_id) e "
        "ON e.tenant_id = o.tenant_id AND e.reference_id = o.id::text "
        "ORDER BY o.tenant_id, o.id")).all()
    emit("affected_found", {"orders": len(rows)})
    classes = {}
    index = {}
    for order_id, tenant, external_id, status, source, meta, count, first_at, last_at in rows:
        meta = meta or {}
        index[tenant] = index.get(tenant, 0) + 1
        alias = f"t{tenant}_a{index[tenant]}"
        events = events_for(conn, tenant, order_id)
        created = ts(meta.get("created_at"))
        confirmed_at = ts(meta.get("cod_confirmed_at")) or (events.get("order.cod.confirmed") or (0, None))[1]
        confirm_keys = [k for k in CONFIRM_KEYS if meta.get(k)]
        confirmed = bool(confirm_keys) or "order.cod.confirmed" in events
        requested = bool(meta.get("nahla_cod_confirmation_sent")) or confirmed
        reminders = list(meta.get("cod_reminders") or [])
        after_confirm = [r for r in reminders if isinstance(r, dict) and confirmed_at
                         and ts(r.get("emitted_at")) and ts(r.get("emitted_at")) > confirmed_at]
        store = store_side(conn, tenant, external_id)
        local = str(status or "").strip().lower()
        label = classify(confirmed, requested, local, store)
        classes[label] = classes.get(label, 0) + 1
        emit("affected", {
            "alias": alias, "local_status": mask(local), "source": mask(source, 20),
            "payment": payment_class(meta), "cod_requested": requested,
            "confirmation_evidence": confirm_keys + (["event:order.cod.confirmed"]
                                                      if "order.cod.confirmed" in events else []),
            "customer_cancel_event": "order.cod.cancelled" in events,
            "confirmed_after_created_min": minutes(created, confirmed_at),
            "first_auto_cancel_after_confirmed_min": minutes(confirmed_at, ts(first_at)),
            "first_auto_cancel_after_created_min": minutes(created, ts(first_at)),
            "auto_cancels": count, "first_auto_cancel": ts(first_at), "last_auto_cancel": ts(last_at),
            "reminders": len(reminders), "reminders_after_confirmation": len(after_confirm),
            "store": store, "class": label,
        })
    emit("affected_summary", {"by_class": classes})


def at_risk(conn):
    autos = conn.execute(text(
        "SELECT tenant_id, config FROM smart_automations "
        "WHERE automation_type = 'cod_confirmation' AND enabled")).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for tenant, config in autos:
        config = config or {}
        try:
            cancel_after = int(config.get("cancel_after_minutes") or 1440)
        except (TypeError, ValueError):
            cancel_after = 1440
        rows = conn.execute(text(
            "SELECT id, status, metadata FROM orders WHERE tenant_id = :t "
            "AND is_abandoned IS NOT TRUE"), {"t": tenant}).all()
        selected = [(i, s, m or {}) for i, s, m in rows if in_sweep(s)]
        confirmed_now, never_asked, unconfirmed = [], 0, 0
        for order_id, status, meta in selected:
            events = events_for(conn, tenant, order_id)
            confirmed = any(meta.get(k) for k in CONFIRM_KEYS) or "order.cod.confirmed" in events
            created = ts(meta.get("created_at"))
            age = minutes(created, now)
            if confirmed:
                confirmed_now.append({
                    "local_status": mask(str(status or "").lower()), "age_min": age,
                    "minutes_to_auto_cancel": None if age is None else cancel_after - age,
                    "already_auto_cancelled_before": bool(meta.get("cod_auto_cancelled_at")),
                    "reminders": len(list(meta.get("cod_reminders") or []))})
            elif meta.get("nahla_cod_confirmation_sent"):
                unconfirmed += 1
            else:
                never_asked += 1
        emit("at_risk", {"tenant": tenant, "cancel_after_minutes": cancel_after,
                         "selected_by_sweep": len(selected), "confirmed": len(confirmed_now),
                         "unconfirmed_cod_requested": unconfirmed, "never_asked": never_asked,
                         "confirmed_orders": confirmed_now[:30]})


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
            for part, fn in (("affected", affected), ("at_risk", at_risk)):
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
