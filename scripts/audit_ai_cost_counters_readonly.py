"""Read-only PostgreSQL accounting audit; emits counts/usage/costs, never content.

Operator supplies NAHLA_AI_COST_AUDIT_DATABASE_URL through their secure runtime.
Example: python scripts/audit_ai_cost_counters_readonly.py --tenant 1 --tenant 33
No backfill, DDL, model call, or production setting change is implemented here.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor


def encode(value):
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def audit(connection, tenants, *, as_of, days, recent):
    connection.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
    since = as_of - timedelta(days=days)
    result = {"read_only": True, "period_timezone": "UTC", "period_start": since,
              "period_end": as_of, "tenant_summaries": [], "recent_runtime_turns": []}
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()["transaction_read_only"] != "on":
            raise RuntimeError("Read-only transaction is required")
        cursor.execute("""
            SELECT tenant_id, token_source, provider, model, pricing_version,
                   COUNT(*) AS ledger_events, SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(total_cost_usd) AS stored_cost_usd,
                   COUNT(*) FILTER (WHERE total_cost_usd IS NULL) AS missing_cost_events,
                   COUNT(*) FILTER (WHERE request_id IS NULL) AS missing_request_id_events,
                   MIN(created_at) AS earliest_event, MAX(created_at) AS latest_event
            FROM ai_usage_events WHERE tenant_id = ANY(%s)
              AND created_at >= %s AND created_at <= %s
            GROUP BY tenant_id, token_source, provider, model, pricing_version
            ORDER BY tenant_id, provider, model, pricing_version, token_source
        """, (tenants, since, as_of))
        result["tenant_summaries"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""
            SELECT COUNT(*) AS duplicate_request_groups FROM (
                SELECT provider, request_id FROM ai_usage_events
                WHERE request_id IS NOT NULL
                GROUP BY provider, request_id HAVING COUNT(*) > 1
            ) AS duplicates
        """)
        result["duplicate_request_groups"] = cursor.fetchone()["duplicate_request_groups"]
        cursor.execute("SELECT to_regclass('public.commerce_runtime_turns') IS NOT NULL AS available")
        result["runtime_turns_available"] = cursor.fetchone()["available"]
        if result["runtime_turns_available"]:
            cursor.execute("""
                WITH ranked AS (
                    SELECT id, tenant_id, admitted_at,
                           ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY admitted_at DESC, id DESC) AS rank
                    FROM commerce_runtime_turns
                    WHERE tenant_id = ANY(%s) AND namespace = 'live'
                      AND admitted_at >= %s AND admitted_at <= %s
                )
                SELECT t.tenant_id, t.id AS runtime_turn_id, t.admitted_at,
                       COUNT(e.id) AS recorded_model_calls,
                       SUM(e.input_tokens) AS recorded_input_tokens,
                       SUM(e.output_tokens) AS recorded_output_tokens,
                       SUM(e.total_cost_usd) AS stored_cost_usd
                FROM ranked t LEFT JOIN ai_usage_events e ON e.tenant_id = t.tenant_id
                    AND e.turn_id = t.id AND e.reason = 'commerce_runtime_pilot'
                    AND e.created_at <= %s
                WHERE t.rank <= %s GROUP BY t.tenant_id, t.id, t.admitted_at
                ORDER BY t.tenant_id, t.admitted_at DESC
            """, (tenants, since, as_of, as_of, recent))
            result["recent_runtime_turns"] = [dict(row) for row in cursor.fetchall()]
    # No call to commit exists: even this SELECT-only transaction is rolled back.
    connection.rollback()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=int, action="append", required=True)
    parser.add_argument("--days", type=int, default=7, choices=range(1, 32))
    parser.add_argument("--recent", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    dsn = os.environ.get("NAHLA_AI_COST_AUDIT_DATABASE_URL")
    if not dsn:
        print(json.dumps({"status": "unavailable", "reason": "audit_connection_not_configured"}))
        return 2
    connection = None
    try:
        connection = psycopg2.connect(dsn, connect_timeout=10,
                                      options="-c statement_timeout=10000 -c default_transaction_read_only=on",
                                      application_name="nahla-ai-cost-readonly")
        result = audit(connection, args.tenant, as_of=datetime.now(timezone.utc),
                       days=args.days, recent=args.recent)
        print(json.dumps(result, default=encode, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 — never print DSNs or SQL parameter details
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__}))
        return 1
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
