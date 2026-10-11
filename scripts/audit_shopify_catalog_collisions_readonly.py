#!/usr/bin/env python3
"""
scripts/audit_shopify_catalog_collisions_readonly.py
────────────────────────────────────────────────────
Read-only audit of Shopify catalog identity collisions and mirror integrity.

Runs in one ``READ ONLY`` transaction and prints counts only — no titles,
names, domains, tokens or identifiers. It adopts, rebinds, repairs or
deletes nothing: legacy rows are reported, never claimed by the mirror.

Checks
──────
schema           foundation (0121) tables, catalog (0122) tables and the
                 ``uq_shopify_connections_id_tenant`` key present?
legacy_hub       shared-catalog rows that look Shopify-sourced
                 (``products.source = 'shopify'``, from the earlier shell) —
                 WARN, never adopted; a Shopify GID stored in
                 ``products.external_id`` or ``product_variants.salla_variant_id``
                 — FAIL (a writer put a provider identity where Salla/generic
                 upserts match by raw id)
raw_id_overlap   same-tenant raw numeric ids shared by the Shopify mirror and
                 Salla/generic hub rows — INFO: expected and harmless, the
                 mirror is keyed by (connection, GID) and no hub writer can
                 reach it; reported so an operator sees the collision surface
mirror_integrity present variants of deleted products or detached items,
                 present levels at deleted locations or of detached items,
                 staged rows of finished runs — FAIL; running runs whose lease
                 expired (closed by the next start) — WARN

Exit codes: 0 no FAIL, 1 at least one FAIL, 2 invocation / connection error.

Usage
─────
  DATABASE_URL="postgresql://..." python scripts/audit_shopify_catalog_collisions_readonly.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Tuple

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"

FOUNDATION = ("shopify_connections", "shopify_shop_leases", "shopify_oauth_states", "shopify_webhook_events",
              "shopify_connection_audit")
CATALOG = ("shopify_catalog_import_runs", "shopify_catalog_staged_records", "shopify_catalog_products",
           "shopify_catalog_inventory_items", "shopify_catalog_variants", "shopify_catalog_locations",
           "shopify_catalog_inventory_levels")


def _tables(cur) -> set:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {row[0] for row in cur.fetchall()}


def _count(cur, sql: str) -> int:
    cur.execute(sql)
    row = cur.fetchone()
    return int(row[0] if row else 0)


def audit(cur) -> Dict[str, Any]:
    """All checks on an open cursor of a READ ONLY transaction."""
    checks: List[Tuple[str, str, int]] = []
    tables = _tables(cur)
    foundation = set(FOUNDATION) <= tables
    catalog = set(CATALOG) <= tables
    key = False
    if "shopify_connections" in tables:
        key = _count(cur, "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_shopify_connections_id_tenant'"
                          " AND conrelid = 'shopify_connections'::regclass") == 1
    schema = {"foundation_0121": foundation, "catalog_0122": catalog, "connection_key": key,
              "partial_catalog": bool(set(CATALOG) & tables) and not catalog}
    if schema["partial_catalog"]:
        checks.append((FAIL, "schema.partial_catalog_tables", len(set(CATALOG) & tables)))

    if "products" in tables:
        n = _count(cur, "SELECT count(*) FROM products WHERE source = 'shopify'")
        checks.append((WARN if n else PASS, "legacy_hub.products_source_shopify_not_adopted", n))
        n = _count(cur, "SELECT count(*) FROM products WHERE external_id LIKE 'gid://shopify/%'")
        checks.append((FAIL if n else PASS, "legacy_hub.products_external_id_is_shopify_gid", n))
    if "product_variants" in tables:
        n = _count(cur, "SELECT count(*) FROM product_variants WHERE salla_variant_id LIKE 'gid://shopify/%'")
        checks.append((FAIL if n else PASS, "legacy_hub.salla_variant_id_is_shopify_gid", n))

    if catalog and "products" in tables:
        n = _count(cur, (
            "SELECT count(*) FROM shopify_catalog_products m JOIN products p ON p.tenant_id = m.tenant_id"
            " AND p.external_id = split_part(m.product_gid, '/', 5)"))
        checks.append((INFO, "raw_id_overlap.products_external_id", n))
    if catalog and "product_variants" in tables:
        n = _count(cur, (
            "SELECT count(*) FROM shopify_catalog_variants m JOIN product_variants v ON v.tenant_id = m.tenant_id"
            " AND v.salla_variant_id = split_part(m.variant_gid, '/', 5)"))
        checks.append((INFO, "raw_id_overlap.salla_variant_id", n))

    if catalog:
        integrity = {
            "mirror_integrity.present_variants_of_deleted_products": (
                "SELECT count(*) FROM shopify_catalog_variants v JOIN shopify_catalog_products p ON p.id = v.product_id"
                " WHERE v.lifecycle = 'present' AND p.lifecycle <> 'present'"),
            "mirror_integrity.present_variants_of_detached_items": (
                "SELECT count(*) FROM shopify_catalog_variants v JOIN shopify_catalog_inventory_items i"
                " ON i.id = v.inventory_item_id WHERE v.lifecycle = 'present' AND i.lifecycle <> 'present'"),
            "mirror_integrity.present_levels_at_deleted_locations": (
                "SELECT count(*) FROM shopify_catalog_inventory_levels l JOIN shopify_catalog_locations c"
                " ON c.id = l.location_id WHERE l.lifecycle = 'present' AND c.lifecycle <> 'present'"),
            "mirror_integrity.present_levels_of_detached_items": (
                "SELECT count(*) FROM shopify_catalog_inventory_levels l JOIN shopify_catalog_inventory_items i"
                " ON i.id = l.inventory_item_id WHERE l.lifecycle = 'present' AND i.lifecycle <> 'present'"),
            "mirror_integrity.staged_rows_of_finished_runs": (
                "SELECT count(*) FROM shopify_catalog_staged_records s JOIN shopify_catalog_import_runs r"
                " ON r.id = s.run_id WHERE r.status <> 'running'"),
        }
        for label, sql in integrity.items():
            n = _count(cur, sql)
            checks.append((FAIL if n else PASS, label, n))
        n = _count(cur, "SELECT count(*) FROM shopify_catalog_import_runs WHERE status = 'running'"
                        " AND lease_expires_at <= now()")
        checks.append((WARN if n else PASS, "mirror_integrity.running_runs_with_expired_lease", n))
        checks.append((INFO, "mirror.committed_runs",
                       _count(cur, "SELECT count(*) FROM shopify_catalog_import_runs WHERE status = 'committed'")))
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
