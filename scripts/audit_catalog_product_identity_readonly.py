#!/usr/bin/env python3
"""
scripts/audit_catalog_product_identity_readonly.py
──────────────────────────────────────────────────
Read-only audit: catalog product identity readiness for a future partial
unique index on ``(tenant_id, external_id)`` where ``external_id`` is
non-null and non-empty.

Does NOT modify the database. Does NOT add constraints.

Exit codes
──────────
  0 — no FAIL checks; safe to plan partial unique index (WARNs may remain)
  1 — duplicates or empty ``external_id`` rows block a safe migration

Usage
─────
  railway run --service nahla-saas python scripts/audit_catalog_product_identity_readonly.py

  DATABASE_URL="postgresql://..." python scripts/audit_catalog_product_identity_readonly.py
"""
from __future__ import annotations

import os
import sys

import psycopg2
import psycopg2.extras

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

SAMPLE_LIMIT = 20

# (status, label, count, note)
results: list[tuple[str, str, int, str]] = []


def _require_db_url() -> str:
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    return db_url


def _count_groups(cur, sql: str) -> int:
    cur.execute(sql)
    row = cur.fetchone()
    return int(row["cnt"] if row else 0)


def _print_sample_rows(rows: list, *, formatter) -> None:
    for row in rows[:SAMPLE_LIMIT]:
        print(f"  {formatter(row)}")
    if len(rows) > SAMPLE_LIMIT:
        print(f"  … and {len(rows) - SAMPLE_LIMIT} more (sample capped at {SAMPLE_LIMIT})")


def main() -> int:
    db_url = _require_db_url()
    conn = psycopg2.connect(db_url)
    try:
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SET TRANSACTION READ ONLY")

        print("Catalog product identity audit (read-only)")
        print("Target index (not applied here):")
        print("  UNIQUE (tenant_id, external_id)")
        print("  WHERE external_id IS NOT NULL AND external_id != ''")
        print()

        # ── 1. Duplicate (tenant_id, external_id) within tenant ─────────────
        dup_count = _count_groups(
            cur,
            """
            SELECT COUNT(*) AS cnt FROM (
                SELECT 1
                FROM products
                WHERE external_id IS NOT NULL AND external_id != ''
                GROUP BY tenant_id, external_id
                HAVING COUNT(*) > 1
            ) dup_groups
            """,
        )
        cur.execute(
            """
            SELECT tenant_id,
                   external_id,
                   COUNT(*) AS count,
                   array_agg(id ORDER BY id) AS product_ids
            FROM products
            WHERE external_id IS NOT NULL AND external_id != ''
            GROUP BY tenant_id, external_id
            HAVING COUNT(*) > 1
            ORDER BY count DESC, tenant_id, external_id
            LIMIT %s
            """,
            (SAMPLE_LIMIT,),
        )
        dup_rows = cur.fetchall()
        dup_status = FAIL if dup_count else PASS
        results.append(
            (dup_status, "Duplicate (tenant_id, external_id)", dup_count, "duplicate groups")
        )
        print(f"[{dup_status}] Duplicate (tenant_id, external_id) groups: {dup_count}")
        _print_sample_rows(
            dup_rows,
            formatter=lambda r: (
                f"tenant={r['tenant_id']} external_id={r['external_id']!r} "
                f"count={r['count']} product_ids={r['product_ids']}"
            ),
        )

        # ── 2. Empty external_id strings ────────────────────────────────────
        empty_total = _count_groups(
            cur,
            "SELECT COUNT(*) AS cnt FROM products WHERE external_id = ''",
        )
        cur.execute(
            """
            SELECT tenant_id, COUNT(*) AS count
            FROM products
            WHERE external_id = ''
            GROUP BY tenant_id
            ORDER BY count DESC, tenant_id
            LIMIT %s
            """,
            (SAMPLE_LIMIT,),
        )
        empty_rows = cur.fetchall()
        empty_status = FAIL if empty_total else PASS
        results.append(
            (empty_status, "products.external_id = ''", empty_total, "rows with empty string")
        )
        print(f"\n[{empty_status}] Rows with external_id = '': {empty_total}")
        _print_sample_rows(
            empty_rows,
            formatter=lambda r: f"tenant={r['tenant_id']} count={r['count']}",
        )

        # ── 3. Manual products with non-empty external_id ───────────────────
        manual_ext_total = _count_groups(
            cur,
            """
            SELECT COUNT(*) AS cnt
            FROM products
            WHERE source = 'manual'
              AND external_id IS NOT NULL
              AND external_id != ''
            """,
        )
        cur.execute(
            """
            SELECT tenant_id, source, COUNT(*) AS count
            FROM products
            WHERE source = 'manual'
              AND external_id IS NOT NULL
              AND external_id != ''
            GROUP BY tenant_id, source
            ORDER BY count DESC, tenant_id
            LIMIT %s
            """,
            (SAMPLE_LIMIT,),
        )
        manual_ext_rows = cur.fetchall()
        manual_ext_status = WARN if manual_ext_total else PASS
        results.append(
            (
                manual_ext_status,
                "manual products with external_id",
                manual_ext_total,
                "rows (review before migration)",
            )
        )
        print(f"\n[{manual_ext_status}] Manual products with non-empty external_id: {manual_ext_total}")
        _print_sample_rows(
            manual_ext_rows,
            formatter=lambda r: (
                f"tenant={r['tenant_id']} source={r['source']} count={r['count']}"
            ),
        )

        # ── 4. Salla products missing external_id ───────────────────────────
        salla_missing_total = _count_groups(
            cur,
            """
            SELECT COUNT(*) AS cnt
            FROM products
            WHERE source = 'salla'
              AND (external_id IS NULL OR external_id = '')
            """,
        )
        cur.execute(
            """
            SELECT tenant_id, source, COUNT(*) AS count
            FROM products
            WHERE source = 'salla'
              AND (external_id IS NULL OR external_id = '')
            GROUP BY tenant_id, source
            ORDER BY count DESC, tenant_id
            LIMIT %s
            """,
            (SAMPLE_LIMIT,),
        )
        salla_missing_rows = cur.fetchall()
        salla_missing_status = WARN if salla_missing_total else PASS
        results.append(
            (
                salla_missing_status,
                "salla products missing external_id",
                salla_missing_total,
                "rows (data quality — review before migration)",
            )
        )
        print(f"\n[{salla_missing_status}] Salla products with NULL/empty external_id: {salla_missing_total}")
        _print_sample_rows(
            salla_missing_rows,
            formatter=lambda r: (
                f"tenant={r['tenant_id']} source={r['source']} count={r['count']}"
            ),
        )

        # ── 5. Legacy / NULL source with non-empty external_id ──────────────
        cur.execute(
            """
            SELECT COALESCE(source, '<NULL>') AS source, COUNT(*) AS count
            FROM products
            WHERE external_id IS NOT NULL AND external_id != ''
            GROUP BY COALESCE(source, '<NULL>')
            ORDER BY count DESC
            """
        )
        source_breakdown = cur.fetchall()
        null_source_count = next(
            (int(r["count"]) for r in source_breakdown if r["source"] == "<NULL>"),
            0,
        )
        null_source_status = WARN if null_source_count else PASS
        results.append(
            (
                null_source_status,
                "NULL source with external_id",
                null_source_count,
                "rows (legacy — review)",
            )
        )
        print(f"\n[{null_source_status}] Products with external_id by source:")
        for r in source_breakdown[:SAMPLE_LIMIT]:
            print(f"  source={r['source']} count={r['count']}")
        if len(source_breakdown) > SAMPLE_LIMIT:
            print(f"  … and {len(source_breakdown) - SAMPLE_LIMIT} more source buckets")

        # ── 6. Manual products without external_id (documentation only) ─────
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM products
            WHERE source = 'manual'
              AND (external_id IS NULL OR external_id = '')
            """
        )
        manual_null_count = int(cur.fetchone()["count"])
        results.append(
            (
                INFO,
                "manual products without external_id",
                manual_null_count,
                "rows (expected — partial unique allows NULL)",
            )
        )
        print(f"\n[{INFO}] Manual products without external_id: {manual_null_count}")
        print("  (documentation only — does not fail the audit)")

        # ── Context totals ──────────────────────────────────────────────────
        cur.execute("SELECT COUNT(*) AS cnt FROM products")
        total_products = int(cur.fetchone()["cnt"])
        cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM products
            WHERE external_id IS NOT NULL AND external_id != ''
            """
        )
        nonempty_external = int(cur.fetchone()["cnt"])
        cur.execute("SELECT COUNT(DISTINCT tenant_id) AS cnt FROM products")
        tenant_count = int(cur.fetchone()["cnt"])
        print(f"\n[{INFO}] Total products: {total_products} across {tenant_count} tenants")
        print(f"[{INFO}] Products with non-empty external_id: {nonempty_external}")

        # ── Summary ─────────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("AUDIT SUMMARY")
        print("=" * 60)
        for status, label, count, note in results:
            print(f"  [{status:4}] {label}: {count} — {note}")

        failed = [r for r in results if r[0] == FAIL]
        warned = [r for r in results if r[0] == WARN]

        if failed:
            print(
                f"\n  RESULT: NOT READY — resolve {len(failed)} FAIL check(s) "
                "before partial unique index migration."
            )
            ready = False
        elif warned:
            print(
                f"\n  RESULT: READY WITH WARNINGS — {len(warned)} WARN check(s) "
                "should be reviewed before PR #4 migration."
            )
            ready = True
        else:
            print("\n  RESULT: READY — no blocking issues for partial unique index planning.")
            ready = True

        print("\nRun on production:")
        print("  railway run --service nahla-saas python scripts/audit_catalog_product_identity_readonly.py")
        print("\nLocal / staging:")
        print('  DATABASE_URL="postgresql://..." python scripts/audit_catalog_product_identity_readonly.py')

        return 0 if ready else 1

    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
