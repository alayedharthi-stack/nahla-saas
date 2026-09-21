"""Read-only exposure check for the Commerce Runtime's shareable-promotions read.

September 2026. The first deployment carrying ``list_shareable_promotions``
(``3a93b106``, deployment ``b1cff354``, 2026-09-21 15:53Z) returned a personal
coupon — one issued to a single customer — to any customer of the merchant.
This script answers, from the production database and without printing a
single coupon code, phone number or customer name:

* whether the runtime handled any turn since that deployment, and for whom;
* whether any turn, terminal or reserved reply ever named the tool or cited a
  promotion as evidence;
* how many personal codes exist (the exposure potential);
* whether any outbound message, or any reply the runtime reserved, carried a
  personal code of a customer other than the conversation's own.

Every query runs in a session that PostgreSQL itself holds read-only
(``default_transaction_read_only=on``). Output is one ``DB_CHECK=`` JSON line
of counts and timestamps.

Usage: ``DATABASE_URL=... python scripts/operators/coupon_exposure_check.py``
(``NAHLA_EXPOSURE_SINCE`` overrides the window start, ISO-8601 with offset).
"""
from __future__ import annotations

import json
import os
import re

from sqlalchemy import create_engine, text

DEFAULT_SINCE = "2026-09-21 15:53:00+00"   # deployment b1cff354: the first with the tool

PERSONAL = ("(cp.metadata ? 'customer_id' OR cp.metadata ? 'assigned_customer_id' "
            "OR cp.metadata ? 'owner_customer_id')")
BOUND = ("NULLIF(regexp_replace(COALESCE(cp.metadata->>'customer_id', "
         "cp.metadata->>'assigned_customer_id', cp.metadata->>'owner_customer_id', ''), "
         "'[^0-9]', '', 'g'), '')::bigint")
# The runtime's conversation row carries the application conversation id as the
# last segment of its reference (``channel:version:prefix:<id>``).
APP_CONVERSATION = "NULLIF(substring(rc.conversation_ref from '([0-9]+)$'), '')::bigint"

QUERIES = {
    "runtime_turns_ever_all_tenants":
        "SELECT count(*) AS n, min(admitted_at) AS first, max(admitted_at) AS last, "
        "count(DISTINCT tenant_id) AS tenants FROM commerce_runtime_turns",
    "runtime_turns_since_deploy_all_tenants":
        "SELECT count(*) AS n, min(admitted_at) AS first, max(admitted_at) AS last "
        "FROM commerce_runtime_turns WHERE admitted_at >= :since",
    "runtime_turns_since_deploy_by_tenant":
        "SELECT tenant_id, namespace, count(*) AS n FROM commerce_runtime_turns "
        "WHERE admitted_at >= :since GROUP BY 1, 2 ORDER BY 1, 2",
    "runtime_terminals_since_deploy_by_outcome":
        "SELECT processing_outcome, transport_outcome, count(*) AS n "
        "FROM commerce_runtime_turn_terminals WHERE recorded_at >= :since GROUP BY 1, 2 ORDER BY 1, 2",
    "runtime_terminals_ever_naming_the_tool":
        "SELECT count(*) AS n FROM commerce_runtime_turn_terminals "
        "WHERE details::text ILIKE '%list_shareable_promotions%'",
    "runtime_delivery_sequences_since_deploy":
        "SELECT count(*) AS n FROM commerce_runtime_delivery_sequences s "
        "JOIN commerce_runtime_turns t ON t.id = s.turn_id AND t.tenant_id = s.tenant_id "
        "WHERE t.admitted_at >= :since",
    "runtime_delivery_sequences_ever_with_promotion_evidence":
        "SELECT count(*) AS n FROM commerce_runtime_delivery_sequences "
        "WHERE intent_payload::text ILIKE '%promotion:%'",
    "runtime_delivery_attempts_since_deploy":
        "SELECT count(*) AS n, min(reserved_at) AS first, max(reserved_at) AS last "
        "FROM commerce_runtime_delivery_attempts WHERE reserved_at >= :since",
    "coupons_tenant1":
        f"SELECT count(*) AS total, count(*) FILTER (WHERE {PERSONAL}) AS personal "
        "FROM coupons cp WHERE cp.tenant_id = 1",
    "coupons_all_tenants":
        f"SELECT count(*) AS total, count(*) FILTER (WHERE {PERSONAL}) AS personal, "
        f"count(DISTINCT tenant_id) FILTER (WHERE {PERSONAL}) AS tenants_with_personal FROM coupons cp",
    # How many of tenant 1's personal codes belong to a customer the runtime has
    # already talked to (the pilot's allowlisted test identities): says whether a
    # privacy trial between two test accounts can exercise a real personal code.
    "tenant1_personal_codes_bound_to_a_customer_the_runtime_has_served":
        f"SELECT count(*) AS n FROM coupons cp WHERE cp.tenant_id = 1 AND {PERSONAL} "
        f"AND {BOUND} IN (SELECT c.customer_id FROM commerce_runtime_conversations rc "
        f"JOIN conversations c ON c.id = {APP_CONVERSATION} WHERE rc.tenant_id = 1)",
    # Inbounds the runtime recorded durably but no turn ever answered: what the
    # handover 'status' command lists as deferred. Recipient masked, ids kept.
    "deferred_inbound_tenant1":
        "SELECT id, state, reason, barrier_generation, created_at, disposed_at, "
        "left(recipient, 5) || repeat('*', greatest(length(recipient) - 7, 0)) || right(recipient, 2) AS recipient_masked, "
        "left(provider_message_id, 12) || '…' AS provider_message_id_prefix "
        "FROM commerce_runtime_deferred_inbound WHERE tenant_id = 1 ORDER BY id",
    "message_events_since_deploy_tenant1":
        "SELECT count(*) FILTER (WHERE direction = 'outbound') AS outbound, "
        "count(*) FILTER (WHERE direction = 'inbound') AS inbound "
        "FROM message_events WHERE tenant_id = 1 AND created_at >= :since_naive",
    "outbound_bodies_carrying_another_customers_personal_code_since_deploy_all_tenants":
        "SELECT count(*) AS n FROM message_events me "
        "JOIN conversations c ON c.id = me.conversation_id "
        f"JOIN coupons cp ON cp.tenant_id = me.tenant_id AND {PERSONAL} "
        "WHERE me.direction = 'outbound' AND me.created_at >= :since_naive "
        "AND length(cp.code) >= 4 AND me.body ILIKE '%' || cp.code || '%' "
        f"AND {BOUND} IS DISTINCT FROM c.customer_id",
    "runtime_delivery_payloads_carrying_another_customers_personal_code_since_deploy":
        "SELECT count(*) AS n FROM commerce_runtime_delivery_attempts a "
        "JOIN commerce_runtime_conversations rc ON rc.id = a.conversation_id AND rc.tenant_id = a.tenant_id "
        f"JOIN conversations c ON c.id = {APP_CONVERSATION} "
        f"JOIN coupons cp ON cp.tenant_id = a.tenant_id AND {PERSONAL} "
        "WHERE a.reserved_at >= :since AND length(cp.code) >= 4 "
        "AND a.payload::text ILIKE '%' || cp.code || '%' "
        f"AND {BOUND} IS DISTINCT FROM c.customer_id",
}


def _int_list(name: str) -> list[int]:
    raw = str(os.environ.get(name, "") or "").strip()
    return [int(part) for part in re.split(r"[,\s]+", raw) if part]


# Optional, non-secret evidence parameters for one trial turn: the coupon and
# product ids the pilot log line cites, and the application conversation id.
EVIDENCE_QUERIES = {
    "coupons_cited": (
        "SELECT cp.id, left(cp.code, 2) || repeat('*', greatest(length(cp.code) - 2, 0)) AS code_masked, "
        "length(cp.code) AS code_len, cp.description, cp.discount_type, cp.discount_value, cp.expires_at, "
        "cp.source_type, cp.coupon_level, cp.allocation_channel, "
        f"{PERSONAL} AS customer_bound, "
        f"({BOUND} = (SELECT c.customer_id FROM conversations c WHERE c.id = :conversation_id)) "
        "AS bound_to_this_conversations_customer, "
        "cp.metadata->>'active' AS meta_active, cp.metadata->>'is_active' AS meta_is_active, "
        "cp.metadata->>'enabled' AS meta_enabled, cp.metadata->>'usage_limit' AS meta_usage_limit, "
        "cp.metadata->>'usage_count' AS meta_usage_count, cp.metadata->>'min_order_amount' AS meta_min_order, "
        "(SELECT string_agg(k, ',') FROM jsonb_object_keys(coalesce(cp.metadata, '{}'::jsonb)) k) AS metadata_keys, "
        "(SELECT json_agg(json_build_object('type', r.rule_type, 'config', r.rule_config)) "
        " FROM coupon_rules r WHERE r.coupon_id = cp.id) AS rules "
        "FROM coupons cp WHERE cp.tenant_id = 1 AND cp.id = ANY(:coupon_ids) ORDER BY cp.id"),
    "products_cited": (
        "SELECT p.id, p.title AS name, p.price, p.stock_quantity, p.in_stock, p.has_variants, p.catalog_status, p.sync_status, (p.archived_at IS NOT NULL) AS archived, (p.merchant_hidden_at IS NOT NULL) AS hidden, "
        "(SELECT string_agg(k, ',') FROM jsonb_object_keys(coalesce(p.metadata, '{}'::jsonb)) k) AS metadata_keys, "
        "left(coalesce(p.metadata->>'variants', p.metadata->>'options', ''), 300) AS variants_excerpt "
        "FROM products p WHERE p.tenant_id = 1 AND p.id = ANY(:product_ids) ORDER BY p.id"),
    "coupons_cited_raw_fields": (
        "SELECT cp.id, cp.metadata->>'discount_pct' AS meta_discount_pct, cp.metadata->>'used' AS meta_used, "
        "cp.metadata->>'salla_synced' AS meta_salla_synced, cp.metadata->>'sync_direction' AS meta_sync_direction, "
        "cp.metadata->>'sync_status' AS meta_sync_status, cp.metadata->>'target_segment' AS meta_target_segment, "
        "cp.metadata->>'source' AS meta_source, cp.metadata->>'category' AS meta_category "
        "FROM coupons cp WHERE cp.tenant_id = 1 AND cp.id = ANY(:coupon_ids) ORDER BY cp.id"),
    "products_cited_raw_fields": (
        "SELECT p.id, p.metadata->>'in_stock' AS meta_in_stock, p.metadata->>'stock_qty' AS meta_stock_qty, "
        "p.metadata->>'status' AS meta_status, p.metadata->>'price' AS meta_price, p.metadata->>'sale_price' AS meta_sale_price, "
        "p.metadata->>'regular_price' AS meta_regular_price, (p.metadata ? 'product_url') AS has_product_url, "
        "(p.metadata ? 'image_url') AS has_image_url, jsonb_array_length(coalesce(p.metadata->'variants', '[]'::jsonb)) AS variant_count, "
        "(SELECT count(*) FROM jsonb_array_elements(coalesce(p.metadata->'variants', '[]'::jsonb)) v "
        " WHERE (v->>'in_stock')::boolean) AS variants_in_stock "
        "FROM products p WHERE p.tenant_id = 1 AND p.id = ANY(:product_ids) ORDER BY p.id"),
    # Whether the model could have read a size claim from the product text it was
    # given (the search view carries title and description, never variants).
    "products_cited_text_hints": (
        "SELECT p.id, length(coalesce(p.description, '')) AS description_len, "
        "(coalesce(p.description, '') ILIKE '%36%') AS description_mentions_36, "
        "(coalesce(p.description, '') ILIKE '%مقاس%') AS description_mentions_size_word, "
        "(coalesce(p.title, '') ILIKE '%36%') AS title_mentions_36, "
        "(coalesce(p.title, '') ILIKE '%أسود%' OR coalesce(p.description, '') ILIKE '%أسود%') AS mentions_black "
        "FROM products p WHERE p.tenant_id = 1 AND p.id = ANY(:product_ids) ORDER BY p.id"),
    # Whether a colour the reply named could come from the product record the
    # tool projects (image url, tags) or only from variant data it does not.
    "products_cited_colour_hints": (
        "SELECT p.id, (coalesce(p.metadata->>'image_url', '') ILIKE '%black%') AS image_url_black, "
        "(coalesce(p.metadata->'variants', '[]'::jsonb)::text ILIKE '%أسود%') AS variants_black, "
        "(coalesce(p.metadata->'options', '[]'::jsonb)::text ILIKE '%أسود%') AS options_black, "
        "(coalesce(p.metadata->'tags', '[]'::jsonb)::text ILIKE '%أسود%') AS tags_black, "
        "(SELECT string_agg(DISTINCT v->'options'->>'اللون', ',') "
        " FROM jsonb_array_elements(coalesce(p.metadata->'variants', '[]'::jsonb)) v) AS variant_colours "
        "FROM products p WHERE p.tenant_id = 1 AND p.id = ANY(:product_ids) ORDER BY p.id"),
    "personal_codes_of_this_conversations_customer": (
        "SELECT cp.id, left(cp.code, 2) || repeat('*', greatest(length(cp.code) - 2, 0)) AS code_masked, "
        "cp.expires_at, cp.source_type, cp.coupon_level, cp.allocation_channel, cp.discount_type, "
        "cp.metadata->>'active' AS meta_active, cp.metadata->>'used' AS meta_used, "
        "cp.metadata->>'usage_limit' AS meta_usage_limit, cp.metadata->>'usage_count' AS meta_usage_count, "
        "cp.metadata->>'status' AS meta_status, cp.metadata->>'issued_channel' AS meta_issued_channel, "
        "(SELECT count(*) FROM coupon_rules r WHERE r.coupon_id = cp.id) AS rule_count "
        f"FROM coupons cp WHERE cp.tenant_id = 1 AND {PERSONAL} "
        f"AND {BOUND} = (SELECT c.customer_id FROM conversations c WHERE c.id = :conversation_id) ORDER BY cp.id"),
    "outbound_rows_window_status": (
        "SELECT me.id, me.created_at, me.event_type, length(me.body) AS body_len, "
        "(SELECT string_agg(k, ',') FROM jsonb_object_keys(coalesce(me.metadata, '{}'::jsonb)) k) AS metadata_keys, "
        "me.metadata->'provider_send' AS provider_send, me.metadata->>'status' AS status, "
        "me.metadata->>'delivery_status' AS delivery_status, me.metadata->'wire_attempts' IS NOT NULL AS has_wire_attempts "
        "FROM message_events me WHERE me.tenant_id = 1 AND me.conversation_id = :conversation_id "
        "AND me.direction = 'outbound' AND me.created_at >= :since_naive ORDER BY me.id"),
    "turns_window": (
        "SELECT t.id, t.admitted_at, t.sequence, tt.processing_outcome, tt.transport_outcome, tt.customer_reach, "
        "tt.recorded_at FROM commerce_runtime_turns t "
        "LEFT JOIN commerce_runtime_turn_terminals tt ON tt.turn_id = t.id AND tt.tenant_id = t.tenant_id "
        "WHERE t.admitted_at >= :since ORDER BY t.id"),
    "delivery_window": (
        "SELECT s.id AS sequence_id, s.turn_id, s.intent_kind, s.outcome, s.attempt_count, "
        "(SELECT json_agg(json_build_object('attempt', a.attempt_no, 'kind', a.kind, 'reserved_at', a.reserved_at)) "
        " FROM commerce_runtime_delivery_attempts a WHERE a.sequence_id = s.id) AS attempts, "
        "(SELECT json_agg(json_build_object('kind', r.kind, 'no', r.receipt_no, 'recorded_at', r.recorded_at)) "
        " FROM commerce_runtime_delivery_receipts r WHERE r.sequence_id = s.id) AS receipts "
        "FROM commerce_runtime_delivery_sequences s "
        "JOIN commerce_runtime_turns t ON t.id = s.turn_id AND t.tenant_id = s.tenant_id "
        "WHERE t.admitted_at >= :since ORDER BY s.id"),
}


def _longest_string(value: object) -> str:
    """The longest string inside a JSON value: the reply text of a delivery payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return max((_longest_string(v) for v in value.values()), key=len, default="")
    if isinstance(value, list):
        return max((_longest_string(v) for v in value), key=len, default="")
    return ""


def _diff_summary(intent: str, wire: str) -> dict:
    """How the transmitted text differs from the reserved intent, without either text."""
    first = next((i for i, (a, b) in enumerate(zip(intent, wire)) if a != b), min(len(intent), len(wire)))
    only_intent = intent[first:][: max(0, len(intent) - len(wire))] if len(intent) > len(wire) else ""
    only_wire = wire[first:][: max(0, len(wire) - len(intent))] if len(wire) > len(intent) else ""
    return {"intent_len": len(intent), "wire_len": len(wire), "equal": intent == wire,
            "first_difference_at": first,
            "chars_only_in_intent": [f"U+{ord(ch):04X}" for ch in only_intent[:8]],
            "chars_only_in_wire": [f"U+{ord(ch):04X}" for ch in only_wire[:8]],
            "intent_ends_with_whitespace": intent[-1:].isspace() if intent else False}


def _intent_versus_wire(conn, since: str, since_naive: str, conversation_id: int) -> list[dict]:
    """For each reply the runtime dispatched in the window, compare the reserved
    payload text with the outbound message row the platform persisted."""
    from sqlalchemy import text as _text  # noqa: PLC0415

    rows = conn.execute(_text(
        "SELECT a.id AS attempt_id, a.sequence_id, a.payload, a.reserved_at "
        "FROM commerce_runtime_delivery_attempts a WHERE a.reserved_at >= :since ORDER BY a.id"),
        {"since": since}).mappings().all()
    out = []
    for row in rows:
        intent = _longest_string(row["payload"])
        wire = conn.execute(_text(
            "SELECT body FROM message_events WHERE tenant_id = 1 AND conversation_id = :conversation_id "
            "AND direction = 'outbound' AND created_at >= :since_naive "
            "AND created_at BETWEEN (:reserved_at AT TIME ZONE 'UTC') - interval '1 minute' "
            "AND (:reserved_at AT TIME ZONE 'UTC') + interval '3 minutes' ORDER BY created_at LIMIT 1"),
            {"conversation_id": conversation_id, "since_naive": since_naive,
             "reserved_at": row["reserved_at"]}).scalar()
        summary = _diff_summary(intent, wire or "") if wire is not None else {"wire_row": "not_found"}
        out.append({"attempt_id": row["attempt_id"], "sequence_id": row["sequence_id"],
                    "reserved_at": str(row["reserved_at"]), **summary})
    return out


def _merchant_ai_coupon_policy(engine) -> dict:
    """The merchant's AI coupon policy as the runtime's coupon read applies it."""
    try:
        import sys  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        root = Path(__file__).resolve().parents[2]
        for p in [str(root), str(root / "backend"), str(root / "database")]:
            if p not in sys.path:
                sys.path.insert(0, p)
        from sqlalchemy.orm import Session  # noqa: PLC0415

        from services.coupon_generator import _get_ai_policy  # noqa: PLC0415

        with Session(engine) as session:
            return dict(_get_ai_policy(session, 1))
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        return {"unreadable": type(exc).__name__}


def _since() -> tuple[str, str]:
    since = str(os.environ.get("NAHLA_EXPOSURE_SINCE", "") or DEFAULT_SINCE).strip()
    naive = re.sub(r"(Z|[+-]\d\d(:?\d\d)?)$", "", since).replace("T", " ").strip()
    return since, naive


def main() -> int:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    since, since_naive = _since()
    out: dict = {"since": since}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        out["read_only"] = conn.execute(text("SHOW default_transaction_read_only")).scalar()
        out["db_now"] = str(conn.execute(text("SELECT now()")).scalar())
        for name, sql in QUERIES.items():
            rows = conn.execute(text(sql), {"since": since, "since_naive": since_naive}).mappings().all()
            out[name] = [dict(r) for r in rows]
        coupon_ids = _int_list("NAHLA_EVIDENCE_COUPON_IDS")
        product_ids = _int_list("NAHLA_EVIDENCE_PRODUCT_IDS")
        conversation_id = int(os.environ.get("NAHLA_EVIDENCE_CONVERSATION_ID", "0") or 0)
        if coupon_ids or product_ids or conversation_id:
            params = {"since": since, "since_naive": since_naive, "coupon_ids": coupon_ids,
                      "product_ids": product_ids, "conversation_id": conversation_id}
            for name, sql in EVIDENCE_QUERIES.items():
                rows = conn.execute(text(sql), params).mappings().all()
                out[name] = [dict(r) for r in rows]
            out["intent_versus_wire"] = _intent_versus_wire(conn, since, since_naive, conversation_id)
            out["merchant_ai_coupon_policy"] = _merchant_ai_coupon_policy(engine)
    print("DB_CHECK=" + json.dumps(out, default=str, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
