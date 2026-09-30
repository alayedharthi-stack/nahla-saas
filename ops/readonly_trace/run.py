"""One-shot, scoped, read-only diagnostic; never starts the application.

Question: in tenant 1, conversation 9, the commerce runtime answered «وش طلباتي
السابقة» (turn 112, 2026-09-30 13:25Z) with one order and "you have one order",
while earlier turns (e.g. 92) showed a different order. Before any fix, find
the first layer that diverges:

1. Linkage: the conversation's customer, same tenant, phone match with the
   sender, and the customer profile's recorded order count.
2. Orders: every order in the tenant that the runtime's own identity clauses
   reach (customer_id, or phone in customer_info), plus orders that share an
   external customer profile with them but that those clauses do not reach.
   Per order: internal row id (the id the runtime cites in evidence refs),
   status, source, open/paid/shipped per the resolver, how it is linked, link
   state, and date-like metadata. No order number, total, name or phone.
3. Retrieval now: the runtime's own resolver
   (core.local_order_resolver.resolve_customer_order_context) with the
   runtime's own arguments, for purpose status and shipment: selected id and
   reason, and the full priority list it held but did not hand on.
4. What turn 112 kept: the conversation's durable agent-loop checkpoint, if it
   is still turn 112's: each tool observation's name, outcome, evidence refs and
   the order view's non-identifying fields.
5. The replies: inbound and reply text of turns 85-87, 92 and 112 (digit runs
   of four or more masked).

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
from sqlalchemy.orm import sessionmaker  # noqa: E402

CONFIRMATION = "READ_ONLY_ORDER_LIST_TRACE_V1"
TENANT = 1
CONVERSATION = 9
TURNS = (85, 86, 87, 92, 112)
DIGITS = re.compile(r"\d{4,}")
DATE_KEY = re.compile(r"(date|_at$|time)", re.I)


def emit(kind, value):
    print("READONLY_TRACE=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str),
          flush=True)


def mask(value, limit=900):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def _dates(meta):
    out = {}
    for key, value in (meta or {}).items():
        if DATE_KEY.search(str(key)) and isinstance(value, (str, int, float)) and len(str(value)) <= 40:
            out[str(key)] = str(value)
        elif isinstance(value, dict) and str(key) in {"date", "salla_dates", "dates"}:
            for sub, sv in value.items():
                if isinstance(sv, (str, int, float)) and len(str(sv)) <= 40:
                    out[f"{key}.{sub}"] = str(sv)
    return dict(sorted(out.items())[:12])


def linkage(db, conn):
    from models import Conversation, Customer
    from utils.phone_utils import normalize_phone_compat

    convo = db.query(Conversation).filter(Conversation.id == CONVERSATION,
                                          Conversation.tenant_id == TENANT).one_or_none()
    out = {"conversation_found": convo is not None}
    if convo is None:
        emit("linkage", out)
        return None, ""
    phone = conn.execute(text(
        "SELECT metadata->>'phone' FROM message_events WHERE tenant_id = :t AND conversation_id = :c "
        "AND direction IN ('in','inbound') ORDER BY id DESC LIMIT 1"),
        {"t": TENANT, "c": CONVERSATION}).scalar() or convo.external_id or ""
    recipient = normalize_phone_compat(phone)
    out["conversation_has_customer_id"] = bool(convo.customer_id)
    customer = None
    if convo.customer_id:
        customer = db.query(Customer).filter(Customer.id == int(convo.customer_id),
                                             Customer.tenant_id == TENANT).one_or_none()
    out["customer_found_in_tenant"] = customer is not None
    if customer is not None:
        stored = normalize_phone_compat(customer.normalized_phone or customer.phone)
        out["customer_phone_matches_sender"] = bool(recipient) and stored == recipient
        profile = conn.execute(text(
            "SELECT total_orders, first_order_at, last_order_at FROM customer_profiles "
            "WHERE tenant_id = :t AND customer_id = :c"), {"t": TENANT, "c": customer.id}).first()
        out["profile_total_orders"] = profile[0] if profile else None
        out["profile_first_order_at"] = profile[1] if profile else None
        out["profile_last_order_at"] = profile[2] if profile else None
    emit("linkage", out)
    return customer, recipient


def orders(db, conn, customer, recipient):
    from sqlalchemy import or_
    from core.local_order_resolver import (
        _customer_order_identity_clauses, _is_open_status, _is_paid_status, _is_shipped_status,
        _order_matches_phone, _phone_lookup_keys)
    from models import Order

    customer_id = int(customer.id) if customer is not None else None
    clauses = _customer_order_identity_clauses(Order, phone=recipient, customer_id=customer_id)
    reached = (db.query(Order).filter(Order.tenant_id == TENANT, or_(*clauses))
               .order_by(Order.id.desc()).all()) if clauses else []
    reached_ids = {o.id for o in reached}
    profiles = {o.external_customer_profile_id for o in reached if o.external_customer_profile_id}
    unreached = []
    if profiles:
        unreached = (db.query(Order).filter(Order.tenant_id == TENANT,
                                            Order.external_customer_profile_id.in_(profiles))
                     .order_by(Order.id.desc()).all())
        unreached = [o for o in unreached if o.id not in reached_ids]
    keys = _phone_lookup_keys(recipient)
    shipments = {}
    if reached or unreached:
        ids = [o.id for o in [*reached, *unreached]]
        for oid, status in conn.execute(text(
                "SELECT order_id, status FROM order_shipments WHERE tenant_id = :t AND order_id = ANY(:ids) "
                "ORDER BY id"), {"t": TENANT, "ids": ids}).fetchall():
            shipments[oid] = status
    rows = []
    for o in [*reached, *unreached]:
        meta = dict(o.extra_metadata or {}) if isinstance(o.extra_metadata, dict) else {}
        rows.append({
            "order_row_id": o.id,
            "reached_by_runtime_clauses": o.id in reached_ids,
            "status": o.status,
            "source": o.source,
            "order_source_kind": o.order_source_kind,
            "is_abandoned": bool(o.is_abandoned),
            "open": _is_open_status(o.status),
            "paid": _is_paid_status(o.status),
            "shipped_status": _is_shipped_status(o.status),
            "shipment_row_status": shipments.get(o.id),
            "customer_id_matches": customer_id is not None and o.customer_id == customer_id,
            "customer_id_null": o.customer_id is None,
            "customer_id_other": o.customer_id is not None and o.customer_id != customer_id,
            "phone_matches": _order_matches_phone(o, keys),
            "customer_link_state": o.customer_link_state,
            "has_external_profile": bool(o.external_customer_profile_id),
            "line_item_count": len(o.line_items) if isinstance(o.line_items, list) else None,
            "dates": _dates(meta),
        })
    emit("orders", {"reached": len(reached), "same_profile_not_reached": len(unreached),
                    "open_reached": sum(1 for r in rows if r["reached_by_runtime_clauses"] and r["open"]),
                    "rows": rows})


def retrieval_now(db, customer, recipient):
    from core.local_order_resolver import resolve_customer_order_context

    for intent in (None, "track_order"):
        ctx = resolve_customer_order_context(
            db, tenant_id=TENANT, conversation_id=CONVERSATION,
            customer_id=int(customer.id) if customer is not None else None,
            phone=recipient, intent=intent, order_number=None)
        emit("retrieval_now", {
            "intent": intent,
            "selected_row_id": ctx.selected_order.order_id if ctx.selected_order else None,
            "selected_reason": ctx.selected_reason,
            "active_whatsapp_draft_row_id": (ctx.active_whatsapp_draft.order_id
                                             if ctx.active_whatsapp_draft else None),
            "priority_list_len": len(ctx.orders_by_priority),
            "priority_list": [{"row_id": s.order_id, "status": s.status, "open": s.is_open}
                              for s in ctx.orders_by_priority],
        })


def checkpoint(conn):
    from core.commerce_runtime import conversation_link as cl

    rows = conn.execute(text(
        "SELECT id, conversation_ref, state_payload, updated_at FROM commerce_runtime_conversations "
        "WHERE tenant_id = :t AND namespace = 'live'"), {"t": TENANT}).fetchall()
    for rid, ref, payload, updated_at in rows:
        parsed = cl.parse_conversation_ref(ref)
        if not parsed or int(parsed[1]) != CONVERSATION:
            continue
        loop = (payload or {}).get("agent_loop") if isinstance(payload, dict) else None
        out = {"runtime_conversation_id": rid, "state_updated_at": updated_at,
               "checkpoint_turn_id": (loop or {}).get("turn_id"),
               "checkpoint_phase": (loop or {}).get("phase")}
        observations = []
        for item in (loop or {}).get("observations") or []:
            body = item.get("body") if isinstance(item, dict) else None
            view = None
            if isinstance(body, dict):
                order = body.get("order") if isinstance(body.get("order"), dict) else {}
                view = {"keys": sorted(body.keys()), "status": body.get("status"), "found": body.get("found"),
                        "selection_reason": body.get("selection_reason"),
                        "order_keys": sorted(order.keys()),
                        "order_row_id": order.get("order_id"), "order_status": order.get("status"),
                        "order_status_label": order.get("status_label"),
                        "line_item_count": len(order.get("line_items") or []) if order else None}
            observations.append({"tool": item.get("tool_name"), "ok": item.get("ok"),
                                 "error_code": item.get("error_code"),
                                 "evidence_refs": item.get("evidence_refs"), "body": view})
        out["observations"] = observations
        emit("checkpoint", out)


def replies(conn):
    for turn in TURNS:
        reply = conn.execute(text(
            "SELECT id, created_at, body FROM message_events WHERE tenant_id = :t AND conversation_id = :c "
            "AND direction IN ('out','outbound') AND metadata->>'commerce_runtime_turn_id' = :turn "
            "ORDER BY id LIMIT 1"), {"t": TENANT, "c": CONVERSATION, "turn": str(turn)}).first()
        if reply is None:
            emit("reply", {"turn_id": turn, "found": False})
            continue
        inbound = conn.execute(text(
            "SELECT created_at, body FROM message_events WHERE tenant_id = :t AND conversation_id = :c "
            "AND direction IN ('in','inbound') AND id < :r ORDER BY id DESC LIMIT 1"),
            {"t": TENANT, "c": CONVERSATION, "r": reply[0]}).first()
        emit("reply", {"turn_id": turn, "found": True, "reply_at_utc": reply[1],
                       "inbound_at_utc": inbound[0] if inbound else None,
                       "inbound": mask(inbound[1] if inbound else "", 300), "reply": mask(reply[2])})


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
            db = sessionmaker(bind=conn)()
            customer, recipient = None, ""
            try:
                customer, recipient = linkage(db, conn)
            except Exception as exc:  # noqa: BLE001 - one part failing does not hide the others
                db.rollback()
                emit("part_failed", {"failed": "linkage", "error_type": type(exc).__name__})
            for part, fn in (("orders", lambda: orders(db, conn, customer, recipient)),
                             ("retrieval_now", lambda: retrieval_now(db, customer, recipient)),
                             ("checkpoint", lambda: checkpoint(conn)),
                             ("replies", lambda: replies(conn))):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    emit("part_failed", {"failed": part, "error_type": type(exc).__name__,
                                         "detail": mask(str(exc), 160)})
            emit("status", {"status": "done"})
        return 0
    except Exception as exc:  # raw errors may carry query values; only the type is logged
        emit("status", {"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
