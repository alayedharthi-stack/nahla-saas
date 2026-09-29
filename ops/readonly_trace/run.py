"""One-shot, scoped, read-only diagnostic; never starts the application.

Two questions, both read-only against the production database:

1. Name linkage (tenant 33). In the conversation(s) where the commerce
   runtime's reply addressed the customer with a name the owner says is not
   theirs, did the customer identity system hold an *approved* name for that
   customer, and would the runtime's own name read
   (services.commerce_runtime_pilot._approved_customer_name: conversation ->
   customer -> phone match -> read_customer_identity -> official status) have
   put it into the model's context? Printed: link and match booleans, name
   status, name source, whether an approved name exists, and whether it (or the
   unapproved proposed/profile name) contains the name the reply used. No name,
   phone or customer identifier is printed.

2. The store used for the Salla review. Which tenants are Salla-connected and
   answered by the commerce runtime; what delivery/returns/payment knowledge each
   holds (merchant data: kind, title, body, visibility); what the runtime's own
   store-wide retrieval returns for common questions; and the owner's recent
   test turns in those stores (inbound and reply text, long digit runs masked).

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

CONFIRMATION = "READ_ONLY_NAME_AND_STORE_POLICY_TRACE_V1"
NAME_TENANT = 33
NAME_USED = "أحمد"
SINCE = "2026-09-25"
TURN_SINCE = "2026-09-27"
CANDIDATE_TENANTS = (1, 35)
POLICY_KINDS_HINT = ("shipping", "return", "refund", "exchange", "payment", "cod", "delivery",
                     "zones", "carrier", "custom", "faq", "terms", "warranty")
QUESTIONS = ("كم رسوم التوصيل؟", "توصلون للرياض؟", "كم يوم ياخذ التوصيل؟", "توصلون للكويت؟",
             "سياسة الاسترجاع", "أقدر أرجع المنتج؟", "أقدر أستبدل المقاس؟", "فيه دفع عند الاستلام؟")
DIGITS = re.compile(r"\d{7,}")


def emit(kind, value):
    print("READONLY_TRACE=" + json.dumps({"part": kind, **value}, ensure_ascii=False, default=str),
          flush=True)


def mask(value, limit=500):
    return DIGITS.sub("[digits]", str(value or ""))[:limit]


def name_linkage(db, conn):
    from core.customer_display import approved_personalization_customer_name_or_fallback
    from core.customer_identity_resolver import is_official_name_status, read_customer_identity
    from models import Conversation, Customer
    from utils.phone_utils import normalize_phone_compat

    replies = conn.execute(text(
        "SELECT id, conversation_id, created_at, metadata FROM message_events "
        "WHERE tenant_id = :t AND direction IN ('out','outbound') AND created_at >= :s "
        "AND strpos(coalesce(body,''), :n) > 0 ORDER BY id DESC LIMIT 20"),
        {"t": NAME_TENANT, "s": SINCE, "n": NAME_USED}).fetchall()
    rows = []
    for rid, convo_id, created_at, meta in replies:
        meta = meta if isinstance(meta, dict) else {}
        rows.append({"reply_row": rid, "conversation_id": convo_id, "reply_at_utc": created_at,
                     "runtime_turn_id": meta.get("commerce_runtime_turn_id"),
                     "compose_source": meta.get("compose_source"),
                     "chosen_path": meta.get("chosen_path")})
    emit("name_replies", {"tenant_id": NAME_TENANT, "since": SINCE, "count": len(rows), "rows": rows})

    for convo_id in sorted({r["conversation_id"] for r in rows if r["conversation_id"]}):
        convo = db.query(Conversation).filter(Conversation.id == int(convo_id),
                                              Conversation.tenant_id == NAME_TENANT).one_or_none()
        out = {"conversation_id": convo_id, "conversation_found": convo is not None}
        if convo is None:
            emit("name_linkage", out)
            continue
        inbound_phone = conn.execute(text(
            "SELECT metadata->>'phone' FROM message_events WHERE tenant_id = :t AND conversation_id = :c "
            "AND direction IN ('in','inbound') ORDER BY id DESC LIMIT 1"),
            {"t": NAME_TENANT, "c": convo_id}).scalar()
        recipient = inbound_phone or convo.external_id
        out["conversation_has_customer_id"] = bool(convo.customer_id)
        customer = None
        if convo.customer_id:
            customer = db.query(Customer).filter(Customer.id == int(convo.customer_id),
                                                 Customer.tenant_id == NAME_TENANT).one_or_none()
        out["customer_found_in_tenant"] = customer is not None
        if customer is not None:
            expected = normalize_phone_compat(recipient)
            stored = normalize_phone_compat(customer.normalized_phone or customer.phone)
            out["recipient_normalizes"] = bool(expected)
            out["customer_phone_matches_recipient"] = bool(expected) and stored == expected
            snap = read_customer_identity(customer)
            approved = approved_personalization_customer_name_or_fallback(snap, fallback="")
            meta = dict(customer.extra_metadata or {})
            out.update({
                "name_present": bool(snap.customer_name),
                "name_status": snap.customer_name_status,
                "name_status_official": is_official_name_status(snap.customer_name_status),
                "name_source": snap.customer_name_source,
                "name_status_recorded": bool(meta.get("customer_name_status")),
                "name_updated_at": snap.customer_name_updated_at,
                "approved_name_present": bool(approved),
                "approved_name_contains_used_name": NAME_USED in approved,
                "stored_name_contains_used_name": NAME_USED in snap.customer_name,
                "proposed_name_present": bool(snap.proposed_name),
                "proposed_name_contains_used_name": NAME_USED in snap.proposed_name,
                "display_name_contains_used_name": NAME_USED in snap.display_name,
                "identity_metadata_keys": sorted(k for k in meta if "name" in k),
                "runtime_would_pass_name_now": bool(approved) and out["customer_phone_matches_recipient"],
            })
        emit("name_linkage", out)


def store_identity(conn):
    salla = conn.execute(text(
        "SELECT DISTINCT tenant_id FROM integrations WHERE lower(provider) LIKE '%salla%'")).fetchall()
    ids = sorted({int(r[0]) for r in salla} | set(CANDIDATE_TENANTS) | {NAME_TENANT})
    for tid in ids:
        row = conn.execute(text("SELECT id, name, created_at, is_active FROM tenants WHERE id = :t"),
                           {"t": tid}).fetchone()
        if row is None:
            continue
        integrations = conn.execute(text(
            "SELECT provider, enabled, external_store_id IS NOT NULL FROM integrations WHERE tenant_id = :t"),
            {"t": tid}).fetchall()
        runtime_turns = conn.execute(text(
            "SELECT count(*), min(created_at), max(created_at) FROM message_events WHERE tenant_id = :t "
            "AND direction IN ('out','outbound') AND metadata ? 'commerce_runtime_turn_id' "
            "AND created_at >= :s"), {"t": tid, "s": "2026-09-01"}).fetchone()
        emit("store_identity", {
            "tenant_id": tid, "name": row[1], "created_at": row[2], "is_active": row[3],
            "integrations": [{"provider": p, "enabled": e, "has_store_id": h} for p, e, h in integrations],
            "products": conn.execute(text("SELECT count(*) FROM products WHERE tenant_id = :t"),
                                     {"t": tid}).scalar(),
            "knowledge_sections": conn.execute(text(
                "SELECT count(*) FROM merchant_knowledge_sections WHERE tenant_id = :t"), {"t": tid}).scalar(),
            "runtime_replies_since_sep1": runtime_turns[0], "first_runtime_reply": runtime_turns[1],
            "last_runtime_reply": runtime_turns[2],
        })
    return ids


def store_policies(db, conn, tid):
    from core.knowledge import apply_ai_visible_kb_query_filters
    from models import MerchantKnowledgeSection
    from modules.ai.brain.commerce import product_knowledge_or_comparison as pkc
    from modules.ai.commerce_agent_v2.knowledge_retrieval import normalize_lookup_query

    visible_ids = {int(r.id) for r in apply_ai_visible_kb_query_filters(
        db.query(MerchantKnowledgeSection)).filter(MerchantKnowledgeSection.tenant_id == tid).all()}
    linked = {int(r[0]) for r in conn.execute(text(
        "SELECT DISTINCT section_id FROM merchant_knowledge_section_products l "
        "JOIN merchant_knowledge_sections s ON s.id = l.section_id WHERE s.tenant_id = :t"),
        {"t": tid}).fetchall()}
    sections = db.query(MerchantKnowledgeSection).filter(MerchantKnowledgeSection.tenant_id == tid) \
        .order_by(MerchantKnowledgeSection.priority.asc(), MerchantKnowledgeSection.id.asc()).all()
    store = conn.execute(text(
        "SELECT same_day_delivery_enabled, pickup_enabled, store_address IS NOT NULL "
        "FROM tenants WHERE id = :t"), {"t": tid}).fetchone()
    emit("store_delivery_fields", {"tenant_id": tid, "same_day_delivery_enabled": store[0],
                                   "pickup_enabled": store[1], "store_address_present": store[2]})
    for s in sections:
        kind = str(s.kind or "")
        policy_like = any(h in kind for h in POLICY_KINDS_HINT)
        emit("store_section", {
            "tenant_id": tid, "section_id": s.id, "kind": kind, "title": s.title,
            "body": mask(s.body, 600) if policy_like else None, "body_chars": len(str(s.body or "")),
            "is_active": s.is_active, "ai_status": s.ai_status,
            "deleted": getattr(s, "deleted_at", None) is not None,
            "ai_visible": int(s.id) in visible_ids, "product_linked": int(s.id) in linked,
            "priority": s.priority,
        })
    for question in QUESTIONS:
        normalized = normalize_lookup_query(question)
        payload = pkc.retrieve_catalog_candidate_kb_sections(
            db, tid, subject=normalized, message=normalized, include_merchant_facts=True)
        emit("store_retrieval", {
            "tenant_id": tid, "question": question, "normalized": normalized,
            "succeeded": not payload.get("kb_retrieval_failed", False),
            "sections": [{"section_id": r.get("section_id"), "title": r.get("title"),
                          "kind": r.get("kind"), "match_score": r.get("match_score")}
                         for r in payload.get("kb_sections") or []],
        })


def store_turns(conn, tid):
    replies = conn.execute(text(
        "SELECT id, conversation_id, created_at, body, metadata FROM message_events "
        "WHERE tenant_id = :t AND direction IN ('out','outbound') AND metadata ? 'commerce_runtime_turn_id' "
        "AND created_at >= :s ORDER BY id DESC LIMIT 30"), {"t": tid, "s": TURN_SINCE}).fetchall()
    for rid, convo_id, created_at, body, meta in reversed(replies):
        inbound = conn.execute(text(
            "SELECT body, created_at FROM message_events WHERE tenant_id = :t AND conversation_id = :c "
            "AND direction IN ('in','inbound') AND id < :r ORDER BY id DESC LIMIT 1"),
            {"t": tid, "c": convo_id, "r": rid}).fetchone()
        emit("store_turn", {
            "tenant_id": tid, "conversation_id": convo_id, "reply_at_utc": created_at,
            "turn_id": (meta or {}).get("commerce_runtime_turn_id"),
            "inbound": mask(inbound[0] if inbound else "", 300),
            "reply": mask(body, 900),
        })


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
            for part, fn in (("name_linkage", lambda: name_linkage(db, conn)),):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - one part failing does not hide the others
                    db.rollback()
                    emit("part_failed", {"failed": part, "error_type": type(exc).__name__})
            try:
                ids = store_identity(conn)
            except Exception as exc:  # noqa: BLE001
                emit("part_failed", {"failed": "store_identity", "error_type": type(exc).__name__})
                ids = list(CANDIDATE_TENANTS)
            for tid in ids:
                if tid == NAME_TENANT:
                    continue
                for part, fn in (("store_policies", lambda: store_policies(db, conn, tid)),
                                 ("store_turns", lambda: store_turns(conn, tid))):
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        emit("part_failed", {"failed": part, "tenant_id": tid,
                                             "error_type": type(exc).__name__})
            emit("status", {"status": "done"})
        return 0
    except Exception as exc:  # raw errors may carry query values; only the type is logged
        emit("status", {"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
