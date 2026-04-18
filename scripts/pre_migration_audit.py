"""
Pre-migration audit: check for conflicts before adding DB constraints.
Run via: railway run --service nahla-saas python scripts/pre_migration_audit.py
"""
import os, psycopg2, psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DB_URL)
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

results = []

# ── 1. Duplicate (tenant_id, phone) in customers ─────────────────────────────
cur.execute("""
    SELECT tenant_id, phone, COUNT(*) AS cnt,
           array_agg(id ORDER BY id) AS customer_ids
    FROM customers
    WHERE phone IS NOT NULL AND phone != ''
    GROUP BY tenant_id, phone
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
    LIMIT 20
""")
rows = cur.fetchall()
status = FAIL if rows else PASS
results.append((status, "UNIQUE(tenant_id, phone)", len(rows), "duplicates found"))
print(f"\n[{status}] Duplicate (tenant_id, phone) pairs: {len(rows)}")
for r in rows:
    print(f"  tenant={r['tenant_id']} phone={r['phone']} count={r['cnt']} ids={r['customer_ids']}")

# ── 2. Cross-tenant same phone ────────────────────────────────────────────────
cur.execute("""
    SELECT phone, COUNT(DISTINCT tenant_id) AS tc
    FROM customers
    WHERE phone IS NOT NULL AND phone != ''
    GROUP BY phone
    HAVING COUNT(DISTINCT tenant_id) > 1
    LIMIT 10
""")
rows2 = cur.fetchall()
status2 = WARN if rows2 else PASS
results.append((status2, "Cross-tenant phone sharing", len(rows2), "phones shared across tenants (expected)"))
print(f"\n[{status2}] Cross-tenant shared phones: {len(rows2)}")
for r in rows2:
    print(f"  phone={r['phone']} tenant_count={r['tc']}")

# ── 3. Duplicate salla_id within same tenant ──────────────────────────────────
cur.execute("""
    SELECT tenant_id,
           metadata->>'salla_id' AS salla_id,
           COUNT(*) AS cnt,
           array_agg(id ORDER BY id) AS customer_ids
    FROM customers
    WHERE metadata->>'salla_id' IS NOT NULL
      AND metadata->>'salla_id' != ''
    GROUP BY tenant_id, metadata->>'salla_id'
    HAVING COUNT(*) > 1
    LIMIT 20
""")
rows3 = cur.fetchall()
status3 = FAIL if rows3 else PASS
results.append((status3, "UNIQUE(tenant_id, salla_id) via JSONB", len(rows3), "duplicates"))
print(f"\n[{status3}] Duplicate (tenant_id, salla_id) in JSONB: {len(rows3)}")
for r in rows3:
    print(f"  tenant={r['tenant_id']} salla_id={r['salla_id']} count={r['cnt']} ids={r['customer_ids']}")

# ── 4. Customers with no tenant_id ────────────────────────────────────────────
cur.execute("SELECT COUNT(*) AS cnt FROM customers WHERE tenant_id IS NULL")
cnt4 = cur.fetchone()["cnt"]
status4 = FAIL if cnt4 else PASS
results.append((status4, "customers.tenant_id NOT NULL", cnt4, "NULL tenant_id rows"))
print(f"\n[{status4}] Customers with NULL tenant_id: {cnt4}")

# ── 5. Integrations: existing unique constraint in DB ────────────────────────
cur.execute("""
    SELECT conname, contype, pg_get_constraintdef(oid) AS def
    FROM pg_constraint
    WHERE conrelid = 'integrations'::regclass
    ORDER BY conname
""")
print(f"\n[INFO] Integrations constraints:")
for r in cur.fetchall():
    print(f"  [{r['contype']}] {r['conname']}: {r['def']}")

# ── 6. Existing indexes on customers ─────────────────────────────────────────
cur.execute("""
    SELECT indexname, indexdef
    FROM pg_indexes
    WHERE tablename = 'customers'
    ORDER BY indexname
""")
print(f"\n[INFO] Customers existing indexes:")
for r in cur.fetchall():
    print(f"  {r['indexname']}: {r['indexdef']}")

# ── 7. customers columns ─────────────────────────────────────────────────────
cur.execute("""
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_name = 'customers'
    ORDER BY ordinal_position
""")
print(f"\n[INFO] Customers columns:")
for r in cur.fetchall():
    print(f"  {r['column_name']} ({r['data_type']}) nullable={r['is_nullable']}")

# ── 8. Total counts ───────────────────────────────────────────────────────────
cur.execute("SELECT COUNT(*) AS cnt FROM customers")
total_customers = cur.fetchone()["cnt"]
cur.execute("SELECT COUNT(DISTINCT tenant_id) AS cnt FROM customers")
tenant_count = cur.fetchone()["cnt"]
print(f"\n[INFO] Total customers: {total_customers} across {tenant_count} tenants")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("AUDIT SUMMARY")
print("="*60)
for (s, label, count, note) in results:
    print(f"  [{s:4}] {label}: {count} {note}")

failed = [r for r in results if r[0] == FAIL]
if failed:
    print(f"\n  ACTION REQUIRED: {len(failed)} FAIL(s) must be resolved before applying constraints.")
else:
    print("\n  All checks PASSED — safe to apply new constraints.")

conn.close()
