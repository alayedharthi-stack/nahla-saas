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
    print("DB_CHECK=" + json.dumps(out, default=str, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
