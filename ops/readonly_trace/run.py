"""One-shot, scoped, read-only diagnostic; never starts the application.

Two parts, both read-only:

``store_status`` — every order the COD sweep ever auto-cancelled (the same
aliases as the consistency trace: tenant + running index by order id). For
each, the order's status **in the store now**, read from the store's API with
the integration's current access token, beside Nahla's local status and its
recorded COD confirmation state, and a proposed correction for the owner to
review. Nothing is changed:

* the database session is read-only (default_transaction_read_only=on);
* the store is only read (GET); no token refresh is attempted, so no new token
  is issued or stored — a rejected or expired token stops that tenant's reads;
* no customer is contacted.

``sweep_check`` — after the sweep fix is deployed: for every tenant with the
COD confirmation automation enabled, which orders the deployed sweep predicate
(services.cod_confirmation.cod_awaits_customer_decision) would still act on,
and which it excludes (customer decided / never asked / already timed out
once); and the sweep's cancels and reminders since the deploy, classified.

Output carries aliases only: never an order id, number, external id, total,
name, phone, address or note; store status names are masked for long digits.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

CONFIRMATION = "READ_ONLY_STORE_STATUS_AND_SWEEP_V1"
# The sweep fix's production deploy (UTC); the sweep check counts from here.
DEPLOYED_AT = os.environ.get("NAHLA_TRACE_SINCE", "").strip()
SALLA_API_BASE = "https://api.salla.dev/admin/v2"
DIGITS = re.compile(r"\d{4,}")
CONFIRM_KEYS = ("cod_confirmed_at", "cod_pushed_external_id", "cod_confirm_requested_at")
DECISION_KEYS = CONFIRM_KEYS + ("cod_cancelled_at", "cod_confirmation_bypassed")
CANCELLED = {"cancelled", "canceled", "canceled_by_admin", "cancelled_by_admin"}
STORE_RETURNED = {"refunded", "restored", "restoring", "returned"}


def emit(kind, value):
    print("READONLY_TRACE=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str),
          flush=True)


def mask(value, limit=40):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def ts(value):
    if isinstance(value, dict):
        value = value.get("date")
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


def payment_class(method):
    value = str(method or "").strip().lower()
    if not value:
        return "missing"
    if value in {"cod", "cash_on_delivery", "cod_payment", "cash"}:
        return "cod"
    if value in {"bank", "bank_transfer"}:
        return "bank_transfer"
    return "other:" + mask(value, 24)


def cod_events(conn, tenant, order_id):
    rows = conn.execute(text(
        "SELECT event_type, count(*) FROM system_events WHERE tenant_id = :t AND reference_id = :r "
        "AND event_type LIKE 'order.cod.%' GROUP BY event_type"),
        {"t": tenant, "r": str(order_id)}).all()
    return {event_type: count for event_type, count in rows}


def nahla_state(meta, events):
    decided = [k for k in DECISION_KEYS if meta.get(k)]
    confirmed = any(meta.get(k) for k in CONFIRM_KEYS) or "order.cod.confirmed" in events
    customer_cancelled = bool(meta.get("cod_cancelled_at")) or "order.cod.cancelled" in events
    asked = bool(meta.get("nahla_cod_confirmation_sent")) or confirmed or customer_cancelled
    if confirmed:
        label = "confirmed"
    elif customer_cancelled:
        label = "customer_cancelled"
    elif asked:
        label = "asked_unanswered"
    else:
        label = "never_asked"
    return label, decided


# ── store_status ────────────────────────────────────────────────────────────

def affected_orders(conn):
    return conn.execute(text(
        "SELECT o.id, o.tenant_id, o.external_id, o.status, o.source, o.metadata, "
        "e.cnt, e.first_at, e.last_at FROM orders o JOIN ("
        "  SELECT tenant_id, reference_id, count(*) AS cnt, min(created_at) AS first_at, "
        "  max(created_at) AS last_at FROM system_events "
        "  WHERE event_type = 'order.cod.auto_cancelled' GROUP BY tenant_id, reference_id) e "
        "ON e.tenant_id = o.tenant_id AND e.reference_id = o.id::text "
        "ORDER BY o.tenant_id, o.id")).all()


def salla_integration(conn, tenant):
    """The tenant's one usable Salla integration, read only (no housekeeping)."""
    rows = conn.execute(text(
        "SELECT id, enabled, config FROM integrations WHERE tenant_id = :t AND provider = 'salla' "
        "ORDER BY id"), {"t": tenant}).all()
    enabled = [(i, c or {}) for i, e, c in rows if e]
    if len(enabled) > 1:
        enabled = [(i, c) for i, c in enabled if c.get("is_canonical")]
    if len(enabled) != 1:
        return None, {"integrations": len(rows), "usable": len(enabled), "state": "ambiguous_or_missing"}
    _id, cfg = enabled[0]
    expires = ts(cfg.get("expires_at") or cfg.get("token_expires_at"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    info = {
        "integrations": len(rows),
        "has_token": bool(cfg.get("api_key")),
        "needs_reauth": bool(cfg.get("needs_reauth")),
        "token_expires_in_min": None if expires is None else int((expires - now).total_seconds() // 60),
    }
    if not cfg.get("api_key") or cfg.get("needs_reauth"):
        info["state"] = "no_usable_token"
        return None, info
    if expires is not None and expires <= now:
        info["state"] = "token_expired_not_refreshed"
        return None, info
    info["state"] = "ok"
    return str(cfg.get("api_key")), info


def store_get(client, token, path):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in (1, 2):
        resp = client.get(SALLA_API_BASE + path, headers=headers)
        if resp.status_code == 429 and attempt == 1:
            time.sleep(5)
            continue
        return resp
    return resp


def store_status_of(raw):
    status = raw.get("status") if isinstance(raw, dict) else None
    if isinstance(status, dict):
        custom = status.get("customized") if isinstance(status.get("customized"), dict) else {}
        return (str(status.get("slug") or "").strip().lower(), mask(status.get("name")),
                mask(custom.get("name")) if custom else None)
    return (str(status or "").strip().lower(), None, None)


def history_summary(payload):
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {"entries": None}
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, dict):
            status = status.get("slug") or status.get("name")
        out.append({"status": mask(status), "at": ts(entry.get("created_at") or entry.get("date"))})
    out.sort(key=lambda e: e["at"] or datetime.min)
    return {"entries": len(out), "last": out[-3:]}


def proposal(local, store_slug, read_state):
    if read_state != "read":
        return "hold_" + read_state
    if store_slug in CANCELLED:
        return "keep_cancelled_store_cancelled"
    if store_slug in STORE_RETURNED:
        return "owner_review_store_returned_or_refunded"
    if local == store_slug:
        return "none_local_matches_store"
    return "restore_local_to_store_status"


def store_status(conn):
    import httpx  # noqa: PLC0415

    rows = affected_orders(conn)
    emit("store_status_found", {"orders": len(rows)})
    tokens, index, summary = {}, {}, {}
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        for order_id, tenant, external_id, status, source, meta, count, first_at, last_at in rows:
            meta = meta or {}
            index[tenant] = index.get(tenant, 0) + 1
            alias = f"t{tenant}_a{index[tenant]}"
            if tenant not in tokens:
                token, info = salla_integration(conn, tenant)
                tokens[tenant] = token
                emit("store_integration", {"tenant": tenant, **info})
            token = tokens[tenant]
            events = cod_events(conn, tenant, order_id)
            nahla, decided = nahla_state(meta, events)
            local = str(status or "").strip().lower()
            row = {
                "alias": alias, "local_status": mask(local), "source": mask(source, 20),
                "local_payment": payment_class(meta.get("payment_method")), "nahla_cod_state": nahla,
                "decision_records": decided + [f"event:{e}" for e in events if e in (
                    "order.cod.confirmed", "order.cod.cancelled")],
                "auto_cancels": count, "first_auto_cancel": ts(first_at), "last_auto_cancel": ts(last_at),
                "created": ts(meta.get("created_at")),
            }
            read_state = "read"
            if str(source or "").strip().lower() != "salla" or not external_id:
                read_state = "not_a_store_order"
            elif token is None:
                read_state = "no_usable_token"
            else:
                try:
                    resp = store_get(client, token, f"/orders/{external_id}")
                    row["store_http"] = resp.status_code
                    if resp.status_code in (401, 403):
                        tokens[tenant] = None  # stop this tenant's reads; never refresh
                        read_state = "token_rejected"
                    elif resp.status_code == 404:
                        read_state = "not_found_in_store"
                    elif resp.status_code != 200:
                        read_state = "store_error"
                    else:
                        raw = (resp.json() or {}).get("data") or {}
                        slug, name, custom = store_status_of(raw)
                        row.update({
                            "store_status": mask(slug), "store_status_name": name,
                            "store_status_custom_name": custom,
                            "store_payment": payment_class(raw.get("payment_method")),
                        })
                        time.sleep(0.4)
                        try:
                            hist = store_get(client, token, f"/orders/{external_id}/histories")
                            row["store_history"] = (history_summary(hist.json()) if hist.status_code == 200
                                                    else {"http": hist.status_code})
                        except Exception as exc:  # noqa: BLE001 - the status read above stands
                            row["store_history"] = {"error_type": type(exc).__name__}
                except Exception as exc:  # noqa: BLE001 - one order's read failing does not hide the rest
                    read_state = "read_failed"
                    row["error_type"] = type(exc).__name__
                time.sleep(0.4)
            row["store_read"] = read_state
            row["proposal"] = proposal(local, row.get("store_status"), read_state)
            summary[row["proposal"]] = summary.get(row["proposal"], 0) + 1
            emit("store_status", row)
    emit("store_status_summary", {"by_proposal": summary})


# ── sweep_check ─────────────────────────────────────────────────────────────

def sweep_check(conn):
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from core.order_queue_classifier import is_pending_confirmation_status  # noqa: PLC0415
    from services.cod_confirmation import (  # noqa: PLC0415
        cod_awaits_customer_decision,
        cod_decided_order_refs,
    )

    since = ts(DEPLOYED_AT)
    if since is None:
        emit("sweep_check", {"state": "no_deploy_time"})
        return
    session = Session(bind=conn)
    autos = conn.execute(text(
        "SELECT tenant_id FROM smart_automations WHERE automation_type = 'cod_confirmation' AND enabled"
    )).all()
    for (tenant,) in autos:
        rows = conn.execute(text(
            "SELECT id, status, metadata FROM orders WHERE tenant_id = :t AND is_abandoned IS NOT TRUE"),
            {"t": tenant}).all()
        orders = [SimpleNamespace(id=i, status=s, extra_metadata=m) for i, s, m in rows]
        selected = [o for o in orders if is_pending_confirmation_status(o.status)]
        decided_refs = cod_decided_order_refs(session, tenant, [o.id for o in selected])
        counts = {"selected_by_status": len(selected), "would_act": 0, "excluded_decided": 0,
                  "excluded_never_asked": 0, "excluded_timed_out_once": 0}
        independent = {}
        violations = 0
        for order in selected:
            meta = order.extra_metadata if isinstance(order.extra_metadata, dict) else {}
            awaits = cod_awaits_customer_decision(order, decided_refs=decided_refs)
            acts = awaits and not meta.get("cod_auto_cancelled_at")
            label, _ = nahla_state(meta, cod_events(conn, tenant, order.id))
            independent[label] = independent.get(label, 0) + 1
            if acts:
                counts["would_act"] += 1
                # The COD checkout's own waiting state is a question asked even
                # without the send stamp; every other unstamped order was never
                # asked, and a decided one is never acted on.
                in_checkout_wait = str(order.status or "").strip().lower() == "pending_confirmation"
                if label in ("confirmed", "customer_cancelled") or (
                        label == "never_asked" and not in_checkout_wait):
                    violations += 1
            elif not awaits and (any(meta.get(k) for k in DECISION_KEYS) or str(order.id) in decided_refs):
                counts["excluded_decided"] += 1
            elif not awaits:
                counts["excluded_never_asked"] += 1
            else:
                counts["excluded_timed_out_once"] += 1
        # The sweep's actions since the deploy, by the order's recorded state.
        cancels = conn.execute(text(
            "SELECT reference_id FROM system_events WHERE tenant_id = :t "
            "AND event_type = 'order.cod.auto_cancelled' AND created_at >= :s"),
            {"t": tenant, "s": since}).all()
        cancel_by_state = {}
        for (ref,) in cancels:
            row = conn.execute(text("SELECT metadata FROM orders WHERE tenant_id = :t AND id::text = :r"),
                               {"t": tenant, "r": str(ref)}).first()
            label, _ = nahla_state((row[0] if row else None) or {}, cod_events(conn, tenant, ref))
            cancel_by_state[label] = cancel_by_state.get(label, 0) + 1
        reminders_by_state = {}
        for order in orders:
            meta = order.extra_metadata if isinstance(order.extra_metadata, dict) else {}
            recent = [r for r in (meta.get("cod_reminders") or []) if isinstance(r, dict)
                      and ts(r.get("emitted_at")) and ts(r.get("emitted_at")) >= since]
            if recent:
                label, _ = nahla_state(meta, cod_events(conn, tenant, order.id))
                reminders_by_state[label] = reminders_by_state.get(label, 0) + len(recent)
        emit("sweep_check", {
            "tenant": tenant, "since": since, **counts,
            "selected_by_recorded_state": independent,
            "would_act_on_decided_or_never_asked": violations,
            "auto_cancels_since_deploy_by_state": cancel_by_state,
            "reminders_since_deploy_by_state": reminders_by_state,
        })
    session.close()


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
            for part, fn in (("sweep_check", sweep_check), ("store_status", store_status)):
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
