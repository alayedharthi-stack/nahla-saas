"""One-shot, scoped, read-only diagnostic; never starts the application.

Question: before «عيال محمد عندك» reached the commerce runtime in tenant 33,
did the conversation hold outbound messages the merchant typed personally (in
the WhatsApp Business app on a shared number), and were they inside the window
of history the model was shown?

What it reads: message_events of tenant 33 only, in the observed message's
conversation (plus conversation-less rows carrying that conversation's phone,
as the runtime's own history read includes them). What it prints: for each
row, its direction, event type, the *names* of its metadata keys and a few
non-identifying source fields, a source class derived from them, its time
offset from the observed message, its body length and whether it was inside
the model's history window. It prints no message text, phone number, name or
customer identifier. Rows whose text equals a message already on record from
the owner's screenshots are labelled by that known role, never by their text.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

CONFIRMATION = "READ_ONLY_TENANT_33_HISTORY_TRACE_V1"
TENANT_ID = 33
OBSERVED = "عيال محمد عندك"
HISTORY_LIMIT = 15          # services.commerce_runtime_pilot.HISTORY_LIMIT on main
KNOWN = {"ابداع روعه": "known_social_compliment", "الله يسعدك 🌷": "known_social_reply",
         OBSERVED: "observed_message"}
PRINTABLE_META_KEYS = ("source", "echo_source", "message_origin", "compose_source",
                       "chosen_path", "historical_only", "echo_type", "provider",
                       "final_customer_text_source", "eval_seeded_history")


def emit(value):
    print("T33_HISTORY_TRACE=" + json.dumps(value, ensure_ascii=False, default=str), flush=True)


def source_class(direction, event_type, meta):
    meta = meta or {}
    if direction in ("in", "inbound"):
        return "customer_inbound"
    if event_type == "smb_message_echo" or meta.get("source") == "merchant_mobile_app":
        return "merchant_typed_in_business_app"
    if event_type == "coexistence_history" or meta.get("historical_only"):
        return "imported_business_app_history"
    if meta.get("commerce_runtime_turn_id") is not None:
        return "ai_commerce_runtime_reply"
    if meta.get("compose_source") or meta.get("chosen_path"):
        return "ai_legacy_reply"
    if "template" in str(event_type or "").lower() or meta.get("template_name"):
        return "template_or_campaign"
    return "outbound_unclassified"


def variants(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    out = {str(phone or "").strip(), digits, "+" + digits}
    if digits.startswith("966"):
        out |= {"0" + digits[3:], digits[3:]}
    return sorted(v for v in out if v)


def main():
    if os.environ.get("NAHLA_HISTORY_TRACE_CONFIRM") != CONFIRMATION:
        emit({"status": "idle"})
        return 0
    try:
        raw = os.environ.get("DATABASE_URL", "")
        parsed = make_url(raw.replace("postgres://", "postgresql://", 1))
        if (parsed.host, parsed.port or 5432, parsed.database) != (
                "postgres-ancu.railway.internal", 5432, "railway"):
            emit({"status": "target_refused"})
            return 1
        engine = create_engine(parsed, connect_args={
            "options": "-c default_transaction_read_only=on -c statement_timeout=15000"})
        with engine.connect() as conn:
            read_only = conn.execute(text("SHOW default_transaction_read_only")).scalar()
            if read_only != "on":
                emit({"status": "not_read_only"})
                return 1
            observed = conn.execute(text(
                "SELECT id, conversation_id, created_at FROM message_events "
                "WHERE tenant_id = :t AND direction IN ('in','inbound') AND btrim(body) = :b "
                "ORDER BY id"), {"t": TENANT_ID, "b": OBSERVED}).fetchall()
            report = {"status": "done", "read_only": read_only, "tenant_id": TENANT_ID,
                      "history_limit": HISTORY_LIMIT, "occurrences": len(observed), "traces": []}
            for occ_index, (obs_id, convo_id, obs_at) in enumerate(observed):
                phone = conn.execute(text(
                    "SELECT external_id FROM conversations WHERE id = :c AND tenant_id = :t"),
                    {"c": convo_id, "t": TENANT_ID}).scalar()
                rows = conn.execute(text(
                    "SELECT id, conversation_id, direction, event_type, body, created_at, metadata "
                    "FROM message_events WHERE tenant_id = :t AND id <= :o AND ("
                    "  conversation_id = :c OR (conversation_id IS NULL "
                    "  AND metadata->>'phone' = ANY(:p))) ORDER BY id"),
                    {"t": TENANT_ID, "o": obs_id, "c": convo_id, "p": variants(phone)}).fetchall()
                before = [r for r in rows if r[0] != obs_id]
                window_ids = {r[0] for r in before[-HISTORY_LIMIT:]}
                out_rows = []
                for rid, rconv, direction, event_type, body, created_at, meta in before:
                    meta = meta if isinstance(meta, dict) else {}
                    body_s = str(body or "")
                    out_rows.append({
                        "order": len(out_rows) + 1,
                        "minutes_before_observed": round((obs_at - created_at).total_seconds() / 60, 1),
                        "created_at_utc": created_at,
                        "scope": "conversation" if rconv == convo_id else "phone_only_row",
                        "direction": direction,
                        "event_type": event_type,
                        "source_class": source_class(direction, event_type, meta),
                        "meta": {k: meta.get(k) for k in PRINTABLE_META_KEYS if k in meta},
                        "meta_keys": sorted(meta.keys()),
                        "has_commerce_runtime_turn_id": meta.get("commerce_runtime_turn_id") is not None,
                        "body_chars": len(body_s),
                        "known_as": KNOWN.get(body_s.strip()),
                        "in_model_history_window": rid in window_ids,
                    })
                obs_meta = conn.execute(text("SELECT metadata FROM message_events WHERE id = :o"),
                                        {"o": obs_id}).scalar()
                classes = sorted({x["source_class"] for x in out_rows})
                report["traces"].append({
                    "occurrence": occ_index + 1,
                    "observed_at_utc": obs_at,
                    "observed_meta_keys": sorted(obs_meta.keys()) if isinstance(obs_meta, dict) else [],
                    "conversation_ref": hashlib.sha256(f"33:{convo_id}".encode()).hexdigest()[:10],
                    "rows_before": len(out_rows),
                    "rows_before_by_source": {c: sum(1 for x in out_rows if x["source_class"] == c)
                                              for c in classes},
                    "window_by_source": {c: sum(1 for x in out_rows if x["source_class"] == c
                                                and x["in_model_history_window"]) for c in classes},
                    "rows": out_rows[-40:],
                })
            emit(report)
        return 0
    except Exception as exc:  # raw errors may carry query values; only the type is logged
        emit({"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
