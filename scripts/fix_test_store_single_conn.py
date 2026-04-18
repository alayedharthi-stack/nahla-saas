"""
Fix (v3): single connection with savepoints — clean orphan tenants 49/50/51
"""
import os, psycopg2, psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DB_URL)
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

ORPHAN_TENANTS = [49, 50, 51]

# Order matters: child tables before parent tables
DEPENDENT_TABLES = [
    "message_events",          # child of conversations
    "merchant_addons",
    "merchant_widgets",
    "whatsapp_connections",
    "whatsapp_usage",
    "tenant_settings",
    "automation_events",
    "webhook_events",
    "campaigns",
    "conversations",
    "coupon_events",
    "coupons",
    "orders",
    "customer_profiles",       # child of customers
    "customers",
    "products",
    "product_interests",
    "automation_rules",
    "conversation_traces",
    "integrity_events",
    "webhook_guardian_log",
    "system_events",
    "store_sync_jobs",
    "billing_subscriptions",
    "store_knowledge_snapshots",
    "smart_automations",
    "users",
]

# ── Step 2: Delete orphan integrations ───────────────────────
print("\n== STEP 2: Delete orphan Salla integrations ==")
cur.execute("SAVEPOINT sp2")
try:
    cur.execute("DELETE FROM integrations WHERE provider='salla' AND tenant_id=ANY(%s) RETURNING id, tenant_id",
                (ORPHAN_TENANTS,))
    for r in cur.fetchall():
        print(f"  Deleted integration id={r['id']} tenant={r['tenant_id']}")
    cur.execute("RELEASE SAVEPOINT sp2")
except Exception as e:
    cur.execute("ROLLBACK TO SAVEPOINT sp2")
    print(f"  Error: {e}")

# ── Step 3: Cascade-delete each orphan tenant ─────────────────
print("\n== STEP 3: Cascade-delete orphan tenants ==")
for tid in ORPHAN_TENANTS:
    cur.execute("SAVEPOINT sp_tenant_%d" % tid)
    try:
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tid,))
        row = cur.fetchone()
        if not row:
            print(f"  Tenant {tid}: not found, skipping.")
            cur.execute("RELEASE SAVEPOINT sp_tenant_%d" % tid)
            continue
        print(f"\n  Tenant {tid} ({row['name']}):")

        for tbl in DEPENDENT_TABLES:
            cur.execute("SAVEPOINT sp_tbl")
            try:
                # Tables without tenant_id — delete via parent FK
                if tbl == "message_events":
                    cur.execute("""
                        DELETE FROM message_events
                        WHERE conversation_id IN (
                            SELECT id FROM conversations WHERE tenant_id=%s
                        )
                    """, (tid,))
                elif tbl == "customer_profiles":
                    cur.execute("""
                        DELETE FROM customer_profiles
                        WHERE customer_id IN (
                            SELECT id FROM customers WHERE tenant_id=%s
                        )
                    """, (tid,))
                else:
                    cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tid,))
                n = cur.rowcount
                if n > 0:
                    print(f"    Deleted {n} rows from {tbl}")
                cur.execute("RELEASE SAVEPOINT sp_tbl")
            except psycopg2.errors.UndefinedTable:
                cur.execute("ROLLBACK TO SAVEPOINT sp_tbl")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT sp_tbl")
                if "does not exist" not in str(e):
                    print(f"    Skipped {tbl}: {e}")

        cur.execute("DELETE FROM tenants WHERE id=%s RETURNING name", (tid,))
        d = cur.fetchone()
        print(f"  Deleted tenant {tid}: OK")
        cur.execute("RELEASE SAVEPOINT sp_tenant_%d" % tid)

    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_tenant_%d" % tid)
        print(f"  FAILED tenant {tid}: {e}")

# ── Step 4: Final verification ────────────────────────────────
print("\n== STEP 4: Final state ==")
cur.execute("""
    SELECT i.tenant_id, i.enabled, i.external_store_id,
           i.config->>'store_id' AS store_id,
           i.config->>'needs_reauth' AS needs_reauth,
           (i.config ? 'api_key') AS has_token
    FROM integrations i
    WHERE i.provider = 'salla'
    ORDER BY i.tenant_id
""")
for r in cur.fetchall():
    print(f"  tenant={r['tenant_id']} store_ext={r['external_store_id']}/"
          f"{r['store_id']} enabled={r['enabled']} "
          f"needs_reauth={r['needs_reauth']} has_token={r['has_token']}")

cur.execute("SELECT id, name FROM tenants WHERE id=ANY(%s)", (ORPHAN_TENANTS,))
rem = cur.fetchall()
print(f"\n  Orphan tenants remaining: {rem if rem else 'NONE'}")

conn.commit()
cur.close(); conn.close()
print("\nDone.")
