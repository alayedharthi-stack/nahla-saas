#!/usr/bin/env python3
"""
scripts/audit_shopify_privacy_readonly.py
─────────────────────────────────────────
Read-only audit of the Shopify privacy compliance slice and of every store
that could hold Shopify-sourced data.

Runs in one ``READ ONLY`` transaction and prints counts only — never a row,
a domain, a config value, a token, an email, a phone or an identifier. It
creates, repairs, deletes or reattributes nothing.

Checks
──────
schema           foundation (0121), catalog (0122), connection key and the
                 privacy tables (0123) present? A partial privacy schema,
                 privacy tables whose columns differ from revision 0123's, or
                 privacy tables while no ``alembic_version`` records 0123
                 (absent table included: an unrecorded / colliding schema that
                 revision 0123 would refuse) — FAIL
audited_schema   live columns of the audited Shopify tables equal the pinned
                 snapshot of ``AUDIT_VERSION`` — FAIL on drift (bounded
                 assessments are only valid against that snapshot)
legacy           Shopify-sourced rows in legacy / generic stores
                 (integrations, webhook_events incl. raw bodies, products,
                 orders, external customer profiles, customer name
                 provenance, tenant settings) — WARN: any of them blocks a
                 bounded "no structured customer records" outcome
requests         only on a validated privacy schema: requests by known topic
                 and state under fixed labels, anything else counted under
                 ``requests.unknown_topic_or_state`` (stored strings are never
                 printed) (INFO); open requests past their 30-day deadline
                 (WARN); delivery-log and event caps exceeded (FAIL)

Exit codes: 0 no FAIL, 1 at least one FAIL, 2 invocation / connection error.

Usage
─────
  DATABASE_URL="postgresql://..." python scripts/audit_shopify_privacy_readonly.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_REPO = Path(__file__).resolve().parents[1]
for entry in (str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from services.shopify_privacy.audit import AUDITED_SHOPIFY_SCHEMA  # noqa: E402
from services.shopify_privacy.config import AUDIT_VERSION  # noqa: E402
from services.shopify_privacy.models import (  # noqa: E402
    MAX_DELIVERY_ROWS_PER_REQUEST,
    MAX_EVENTS_PER_REQUEST,
    PRIVACY_TABLE_NAMES,
    PRIVACY_TABLES,
    STATES,
    TOPICS,
)

PRIVACY_COLUMNS = {t.name: frozenset(c.name for c in t.columns) for t in PRIVACY_TABLES}

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"

FOUNDATION = ("shopify_connections", "shopify_shop_leases", "shopify_oauth_states", "shopify_webhook_events",
              "shopify_connection_audit")
CATALOG = ("shopify_catalog_import_runs", "shopify_catalog_staged_records", "shopify_catalog_products",
           "shopify_catalog_inventory_items", "shopify_catalog_variants", "shopify_catalog_locations",
           "shopify_catalog_inventory_levels")

# (label, table, required columns, SQL) — counts of Shopify-sourced legacy rows.
LEGACY: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("legacy.integrations_provider_shopify", "integrations", ("provider",),
     "SELECT count(*) FROM integrations WHERE lower(provider) = 'shopify'"),
    ("legacy.webhook_events_provider_shopify", "webhook_events", ("provider",),
     "SELECT count(*) FROM webhook_events WHERE lower(provider) = 'shopify'"),
    ("legacy.webhook_events_shopify_with_raw_body", "webhook_events", ("provider", "raw_body"),
     "SELECT count(*) FROM webhook_events WHERE lower(provider) = 'shopify' AND raw_body IS NOT NULL"),
    ("legacy.products_source_shopify", "products", ("source",),
     "SELECT count(*) FROM products WHERE lower(source) = 'shopify'"),
    ("legacy.orders_source_shopify", "orders", ("source",),
     "SELECT count(*) FROM orders WHERE lower(source) = 'shopify'"),
    ("legacy.external_profiles_of_shopify_integrations", "external_customer_profiles", ("integration_connection_id",),
     "SELECT count(*) FROM external_customer_profiles p JOIN integrations i ON i.id = p.integration_connection_id"
     " WHERE lower(i.provider) = 'shopify'"),
    ("legacy.customer_name_provenance_shopify", "customer_name_provenance", ("source", "last_attempt_source"),
     "SELECT count(*) FROM customer_name_provenance WHERE lower(coalesce(source, '')) LIKE 'shopify%'"
     " OR lower(coalesce(last_attempt_source, '')) LIKE 'shopify%'"),
    ("legacy.tenant_settings_with_shopify_values", "tenant_settings", ("store_settings",),
     "SELECT count(*) FROM tenant_settings WHERE coalesce(store_settings->>'shopify_access_token', '') <> ''"
     " OR coalesce(store_settings->>'shopify_shop_domain', '') <> ''"),
)


def _tables(cur) -> set:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {row[0] for row in cur.fetchall()}


def _columns(cur, table: str) -> frozenset:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema()"
                " AND table_name = %s", (table,))
    return frozenset(row[0] for row in cur.fetchall())


def _count(cur, sql: str) -> int:
    cur.execute(sql)
    row = cur.fetchone()
    return int(row[0] if row else 0)


def audit(cur) -> Dict[str, Any]:
    """All checks on an open cursor of a READ ONLY transaction."""
    checks: List[Tuple[str, str, int]] = []
    tables = _tables(cur)
    privacy_present = set(PRIVACY_TABLE_NAMES) & tables
    key = False
    if "shopify_connections" in tables:
        key = _count(cur, "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_shopify_connections_id_tenant'"
                          " AND conrelid = 'shopify_connections'::regclass") == 1
    versions: set = set()
    if "alembic_version" in tables:
        cur.execute("SELECT version_num FROM alembic_version")
        versions = {row[0] for row in cur.fetchall()}
    schema = {
        "foundation_0121": set(FOUNDATION) <= tables,
        "catalog_0122": set(CATALOG) <= tables,
        "connection_key": key,
        "privacy_0123": set(PRIVACY_TABLE_NAMES) <= tables,
        "alembic_records_0123": "0123" in versions,
        "audit_version": AUDIT_VERSION,
    }
    if privacy_present and not schema["privacy_0123"]:
        checks.append((FAIL, "schema.partial_privacy_tables", len(privacy_present)))
    if privacy_present and "0123" not in versions:
        # No alembic_version at all counts too: the tables were not recorded
        # as built by revision 0123.
        checks.append((FAIL, "schema.privacy_tables_without_revision_0123", len(privacy_present)))
    malformed = sum(1 for t in privacy_present if _columns(cur, t) != PRIVACY_COLUMNS[t])
    schema["privacy_columns_valid"] = bool(schema["privacy_0123"]) and malformed == 0
    if malformed:
        checks.append((FAIL, "schema.privacy_tables_malformed", malformed))

    drift = 0
    for table, expected in AUDITED_SHOPIFY_SCHEMA.items():
        if table in tables and _columns(cur, table) != expected:
            drift += 1
    if set(AUDITED_SHOPIFY_SCHEMA) <= tables:
        checks.append((FAIL if drift else PASS, "audited_schema.tables_with_column_drift", drift))
    else:
        checks.append((INFO, "audited_schema.tables_absent", len(set(AUDITED_SHOPIFY_SCHEMA) - tables)))

    for label, table, columns, sql in LEGACY:
        needed = {table} | ({"integrations"} if "integrations" in sql and table != "integrations" else set())
        if not needed <= tables or not set(columns) <= _columns(cur, table):
            checks.append((INFO, label + ".not_present", 0))
            continue
        n = _count(cur, sql)
        checks.append((WARN if n else PASS, label, n))

    if schema["privacy_columns_valid"]:
        # Fixed labels only: stored strings are compared, never printed.
        for topic in TOPICS:
            for state in STATES:
                cur.execute("SELECT count(*) FROM shopify_privacy_requests WHERE topic = %s AND state = %s",
                            (topic, state))
                n = int(cur.fetchone()[0])
                if n:
                    checks.append((INFO, f"requests.{topic}.{state}", n))
        cur.execute("SELECT count(*) FROM shopify_privacy_requests WHERE topic IS NULL OR state IS NULL"
                    " OR NOT (topic = ANY(%s)) OR NOT (state = ANY(%s))", (list(TOPICS), list(STATES)))
        n = int(cur.fetchone()[0])
        checks.append((FAIL if n else PASS, "requests.unknown_topic_or_state", n))
        n = _count(cur, "SELECT count(*) FROM shopify_privacy_requests WHERE state <> 'completed'"
                        " AND deadline_at <= now()")
        checks.append((WARN if n else PASS, "requests.open_past_deadline", n))
        n = _count(cur, "SELECT count(*) FROM (SELECT request_id FROM shopify_privacy_deliveries GROUP BY request_id"
                        f" HAVING count(*) > {int(MAX_DELIVERY_ROWS_PER_REQUEST)}) x")
        checks.append((FAIL if n else PASS, "requests.delivery_log_over_cap", n))
        n = _count(cur, "SELECT count(*) FROM (SELECT request_id FROM shopify_privacy_request_events"
                        f" GROUP BY request_id HAVING count(*) > {int(MAX_EVENTS_PER_REQUEST) + 1}) x")
        checks.append((FAIL if n else PASS, "requests.event_log_over_cap", n))
    verdict = FAIL if any(c[0] == FAIL for c in checks) else (WARN if any(c[0] == WARN for c in checks) else PASS)
    return {"verdict": verdict, "schema": schema,
            "checks": [{"status": s, "check": label, "count": n} for s, label, n in checks]}


def main() -> int:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print(json.dumps({"verdict": "NOT RUN", "reason": "DATABASE_URL is not set"}))
        return 2
    try:
        import psycopg2  # noqa: PLC0415

        conn = psycopg2.connect(url)
    except Exception as exc:  # noqa: BLE001 — never print the URL or the driver message
        print(json.dumps({"verdict": "NOT RUN", "reason": "connection_failed", "kind": type(exc).__name__}))
        return 2
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            report = audit(cur)
        conn.rollback()
    finally:
        conn.close()
    print(json.dumps(report, indent=2))
    return 1 if report["verdict"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
